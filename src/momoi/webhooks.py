import asyncio
import hmac
import json
import logging
import math
import os
import re
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from aiohttp import web

from .config import WebhookConfig
from .models import AgentReply
from .storage import Store


logger = logging.getLogger(__name__)
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TEMPLATE = re.compile(r"\$\{(inputs|args|config)\.([a-z][a-z0-9_]*)\}")
_INPUT_TYPES = {"string", "integer", "number", "boolean"}


class WorkflowError(ValueError):
    pass


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowError(f"{name} must be an object")
    return {str(key): item for key, item in value.items()}


def _only(mapping: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise WorkflowError(f"{name} has unknown fields: {', '.join(sorted(unknown))}")


def _identifier(value: object, name: str) -> str:
    text = str(value or "")
    if not _ID.fullmatch(text):
        raise WorkflowError(f"{name} must be a lowercase identifier")
    return text


def _input_schema(value: object, name: str) -> dict[str, Any]:
    schema = _mapping(value, name)
    _only(
        schema,
        {"type", "required", "max_length", "pattern", "format", "schemes"},
        name,
    )
    kind = str(schema.get("type") or "")
    if kind not in _INPUT_TYPES:
        raise WorkflowError(f"{name}.type must be string, integer, number, or boolean")
    required = schema.get("required", False)
    if not isinstance(required, bool):
        raise WorkflowError(f"{name}.required must be boolean")
    result: dict[str, Any] = {"type": kind, "required": required}
    if "max_length" in schema:
        limit = int(schema["max_length"])
        if kind != "string" or limit <= 0:
            raise WorkflowError(f"{name}.max_length requires a positive string limit")
        result["max_length"] = limit
    if "pattern" in schema:
        if kind != "string":
            raise WorkflowError(f"{name}.pattern is only valid for strings")
        try:
            re.compile(str(schema["pattern"]))
        except re.error as error:
            raise WorkflowError(f"{name}.pattern is invalid: {error}") from None
        result["pattern"] = str(schema["pattern"])
    if "format" in schema:
        if kind != "string" or schema["format"] != "url":
            raise WorkflowError(f"{name}.format only supports url strings")
        result["format"] = "url"
        schemes = schema.get("schemes", ["http", "https"])
        if not isinstance(schemes, list) or not schemes:
            raise WorkflowError(f"{name}.schemes must be a non-empty array")
        result["schemes"] = [str(item).lower() for item in schemes]
    elif "schemes" in schema:
        raise WorkflowError(f"{name}.schemes requires format: url")
    return result


def _validate_value(value: object, schema: dict[str, Any], name: str) -> object:
    kind = schema["type"]
    if kind == "string":
        valid = isinstance(value, str)
    elif kind == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "number":
        valid = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    else:
        valid = isinstance(value, bool)
    if not valid:
        raise WorkflowError(f"{name} must be {kind}")
    if isinstance(value, str):
        if len(value) > int(schema.get("max_length", len(value))):
            raise WorkflowError(f"{name} is too long")
        if schema.get("pattern") and not re.fullmatch(str(schema["pattern"]), value):
            raise WorkflowError(f"{name} does not match its pattern")
        if schema.get("format") == "url":
            parsed = urlsplit(value)
            if parsed.scheme.lower() not in schema["schemes"] or not parsed.hostname:
                raise WorkflowError(f"{name} must be an allowed absolute URL")
    return value


def _templates(text: str, *, source: str, names: set[str], name: str) -> None:
    stripped = _TEMPLATE.sub("", text)
    if "${" in stripped:
        raise WorkflowError(f"{name} contains an invalid template")
    for scope, key in _TEMPLATE.findall(text):
        if scope != source or key not in names:
            raise WorkflowError(f"{name} references unknown {scope}.{key}")


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise WorkflowError(f"cannot load {path}: {error}") from None
    return _mapping(value, str(path))


def load_catalog(
    workflows_path: Path,
    executors_path: Path,
    config_names: set[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    config_names = config_names or set()
    executor_root = _load_yaml(executors_path)
    _only(executor_root, {"version", "executors"}, str(executors_path))
    if executor_root.get("version") != 1:
        raise WorkflowError(f"{executors_path} requires version: 1")
    executors: dict[str, dict[str, Any]] = {}
    for executor_id, raw in _mapping(
        executor_root.get("executors", {}), "executors"
    ).items():
        executor_id = _identifier(executor_id, "executor id")
        item = _mapping(raw, f"executor {executor_id}")
        _only(
            item,
            {"parameters", "argv", "env", "timeout_seconds"},
            f"executor {executor_id}",
        )
        parameters = {
            _identifier(key, f"executor {executor_id} parameter"): _input_schema(
                value, f"executor {executor_id} parameter {key}"
            )
            for key, value in _mapping(
                item.get("parameters", {}), f"executor {executor_id}.parameters"
            ).items()
        }
        argv = item.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(arg, str) for arg in argv)
        ):
            raise WorkflowError(
                f"executor {executor_id}.argv must be a non-empty string array"
            )
        incompatible = False
        for index, arg in enumerate(argv):
            if "${" in arg:
                match = _TEMPLATE.fullmatch(arg)
                if (
                    match is None
                    or (match.group(1) == "args" and match.group(2) not in parameters)
                    or (
                        match.group(1) == "config"
                        and match.group(2) not in config_names
                    )
                    or match.group(1) == "inputs"
                ):
                    logger.warning(
                        "Skipping incompatible workflow executor %s: argv[%d] has an invalid template",
                        executor_id,
                        index,
                    )
                    incompatible = True
                    break
        if incompatible:
            continue
        env = _mapping(item.get("env", {}), f"executor {executor_id}.env")
        if not all(isinstance(value, str) for value in env.values()):
            raise WorkflowError(f"executor {executor_id}.env values must be strings")
        for key, value in env.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise WorkflowError(
                    f"executor {executor_id}.env has an invalid variable name"
                )
            if "${" in value:
                match = _TEMPLATE.fullmatch(value)
                if (
                    match is None
                    or (match.group(1) == "args" and match.group(2) not in parameters)
                    or (
                        match.group(1) == "config"
                        and match.group(2) not in config_names
                    )
                    or match.group(1) == "inputs"
                ):
                    logger.warning(
                        "Skipping incompatible workflow executor %s: env.%s has an invalid template",
                        executor_id,
                        key,
                    )
                    incompatible = True
                    break
        if incompatible:
            continue
        timeout = float(item.get("timeout_seconds", 180))
        if timeout <= 0:
            raise WorkflowError(
                f"executor {executor_id}.timeout_seconds must be positive"
            )
        executors[executor_id] = {
            "parameters": parameters,
            "argv": argv,
            "env": env,
            "timeout_seconds": timeout,
        }

    if not workflows_path.is_dir():
        raise WorkflowError(f"workflow directory not found: {workflows_path}")
    workflows: dict[str, dict[str, Any]] = {}
    for path in sorted(workflows_path.glob("*.yaml")):
        root = _load_yaml(path)
        _only(root, {"version", "id", "description", "inputs", "steps"}, str(path))
        if root.get("version") != 1:
            raise WorkflowError(f"{path} requires version: 1")
        workflow_id = _identifier(root.get("id"), f"{path}.id")
        if workflow_id in workflows:
            raise WorkflowError(f"duplicate workflow id: {workflow_id}")
        inputs = {
            _identifier(key, f"workflow {workflow_id} input"): _input_schema(
                value, f"workflow {workflow_id} input {key}"
            )
            for key, value in _mapping(
                root.get("inputs", {}), f"workflow {workflow_id}.inputs"
            ).items()
        }
        raw_steps = root.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise WorkflowError(f"workflow {workflow_id}.steps must be non-empty")
        steps: list[dict[str, Any]] = []
        step_ids: set[str] = set()
        incompatible = False
        for index, raw_step in enumerate(raw_steps):
            step = _mapping(raw_step, f"workflow {workflow_id} step {index}")
            step_id = _identifier(step.get("id"), f"workflow {workflow_id} step id")
            if step_id in step_ids:
                raise WorkflowError(
                    f"workflow {workflow_id} has duplicate step id {step_id}"
                )
            step_ids.add(step_id)
            uses = str(step.get("uses") or "")
            if uses == "message":
                _only(
                    step,
                    {"id", "uses", "prompt"},
                    f"workflow {workflow_id} step {step_id}",
                )
                prompt = step.get("prompt")
                if not isinstance(prompt, str) or not prompt.strip():
                    raise WorkflowError(
                        f"workflow {workflow_id} step {step_id}.prompt is required"
                    )
                _templates(
                    prompt,
                    source="inputs",
                    names=set(inputs),
                    name=f"workflow {workflow_id} step {step_id}.prompt",
                )
                steps.append({"id": step_id, "uses": uses, "prompt": prompt})
                continue
            if uses != "exec":
                raise WorkflowError(
                    f"workflow {workflow_id} step {step_id}.uses is invalid"
                )
            _only(
                step,
                {"id", "uses", "executor", "args"},
                f"workflow {workflow_id} step {step_id}",
            )
            executor_id = _identifier(
                step.get("executor"), f"workflow {workflow_id} executor"
            )
            executor = executors.get(executor_id)
            if executor is None:
                logger.warning(
                    "Skipping incompatible workflow %s: executor %s is unavailable",
                    workflow_id,
                    executor_id,
                )
                incompatible = True
                break
            args = _mapping(
                step.get("args", {}), f"workflow {workflow_id} step {step_id}.args"
            )
            if set(args) != set(executor["parameters"]):
                raise WorkflowError(
                    f"workflow {workflow_id} step {step_id}.args must match executor parameters"
                )
            for key, value in args.items():
                if not isinstance(value, str):
                    raise WorkflowError(
                        f"workflow {workflow_id} step {step_id}.args.{key} must be a template"
                    )
                match = _TEMPLATE.fullmatch(value)
                if (
                    match is None
                    or match.group(1) != "inputs"
                    or match.group(2) not in inputs
                ):
                    raise WorkflowError(
                        f"workflow {workflow_id} step {step_id}.args.{key} must reference one input"
                    )
            steps.append(
                {
                    "id": step_id,
                    "uses": uses,
                    "executor": executor_id,
                    "args": args,
                }
            )
        if incompatible:
            continue
        workflows[workflow_id] = {"id": workflow_id, "inputs": inputs, "steps": steps}
    if not workflows:
        logger.warning("No valid workflow YAML files found in %s", workflows_path)
    return workflows, executors


def bind_workflow(
    workflow: dict[str, Any],
    executors: dict[str, dict[str, Any]],
    supplied: object,
    config_values: dict[str, str],
) -> dict[str, Any]:
    values = _mapping(supplied, "request body")
    schemas = workflow["inputs"]
    unknown = set(values) - set(schemas)
    if unknown:
        raise WorkflowError(f"unknown inputs: {', '.join(sorted(unknown))}")
    normalized: dict[str, object] = {}
    for name, schema in schemas.items():
        if name not in values:
            if schema["required"]:
                raise WorkflowError(f"missing required input: {name}")
            continue
        normalized[name] = _validate_value(values[name], schema, f"input {name}")

    def render_prompt(template: str) -> str:
        return _TEMPLATE.sub(
            lambda match: str(normalized.get(match.group(2), "")), template
        )

    bound_steps: list[dict[str, Any]] = []
    for step in workflow["steps"]:
        if step["uses"] == "message":
            bound_steps.append(
                {
                    "id": step["id"],
                    "uses": "message",
                    "prompt": render_prompt(step["prompt"]),
                }
            )
            continue
        executor = executors[step["executor"]]
        arguments: dict[str, object] = {}
        for name, template in step["args"].items():
            input_name = _TEMPLATE.fullmatch(template).group(2)  # type: ignore[union-attr]
            if input_name not in normalized:
                raise WorkflowError(f"missing input required by executor: {input_name}")
            arguments[name] = _validate_value(
                normalized[input_name],
                executor["parameters"][name],
                f"executor argument {name}",
            )

        def resolve(token: str) -> str:
            match = _TEMPLATE.fullmatch(token)
            if match is None:
                return token
            source, name = match.groups()
            value = arguments[name] if source == "args" else config_values[name]
            return str(value)

        bound_steps.append(
            {
                "id": step["id"],
                "uses": "exec",
                "executor": step["executor"],
                "argv": [resolve(arg) for arg in executor["argv"]],
                "env": {key: resolve(value) for key, value in executor["env"].items()},
                "timeout_seconds": executor["timeout_seconds"],
            }
        )
    return {"version": 1, "workflow": workflow["id"], "steps": bound_steps}


class WebhookService:
    def __init__(
        self,
        config: WebhookConfig,
        channel_variables: dict[str, str],
        store: Store,
        complete_turn: Callable[[str, str], Awaitable[AgentReply]],
        wake_outbox: Callable[[], None],
        primary_channel: str = "",
    ) -> None:
        if config.workflows is None or config.executors is None:
            raise WorkflowError("webhook paths are not configured")
        self.config = config
        self.channel_variables = channel_variables
        self.store = store
        self.complete_turn = complete_turn
        self.wake_outbox = wake_outbox
        self.primary_channel = primary_channel
        self.workflows, self.executors = load_catalog(
            config.workflows, config.executors, set(channel_variables)
        )
        self.changed = asyncio.Event()

    async def run_api(self, stop: asyncio.Event) -> None:
        @web.middleware
        async def authenticate(
            request: web.Request,
            handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
        ) -> web.StreamResponse:
            authorization = request.headers.get("Authorization", "")
            expected = f"Bearer {self.config.token}"
            if not hmac.compare_digest(authorization, expected):
                return web.json_response({"error": "unauthorized"}, status=401)
            return await handler(request)

        app = web.Application(client_max_size=64 * 1024, middlewares=[authenticate])
        app.router.add_post("/webhooks/{workflow_id}", self._post)
        app.router.add_get("/webhook-runs/{run_id}", self._get)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.config.host, self.config.port)
        await site.start()
        logger.info(
            "Webhook API listening host=%s port=%d", self.config.host, self.config.port
        )
        try:
            await stop.wait()
        finally:
            await runner.cleanup()

    async def _post(self, request: web.Request) -> web.Response:
        workflow_id = request.match_info["workflow_id"]
        workflow = self.workflows.get(workflow_id)
        if workflow is None:
            return web.json_response({"error": "workflow_not_found"}, status=404)
        try:
            body = await request.json()
            plan = bind_workflow(workflow, self.executors, body, self.channel_variables)
            key = request.headers.get("Idempotency-Key")
            if key is not None and (
                not key or len(key) > 200 or any(ord(char) < 32 for char in key)
            ):
                raise WorkflowError("Idempotency-Key is invalid")
            run, _ = self.store.create_webhook_run(workflow_id, key, plan)
        except (json.JSONDecodeError, WorkflowError, TypeError, ValueError) as error:
            return web.json_response(
                {"error": "invalid_request", "detail": str(error)}, status=400
            )
        self.changed.set()
        return web.json_response(
            {
                "run_id": run["id"],
                "workflow": run["workflow_id"],
                "state": run["state"],
            },
            status=202,
        )

    async def _get(self, request: web.Request) -> web.Response:
        run = self.store.webhook_run(request.match_info["run_id"])
        if run is None:
            return web.json_response({"error": "run_not_found"}, status=404)
        return web.json_response(run)

    async def run_worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            run = self.store.claim_webhook_run()
            if run is None:
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=1)
                except TimeoutError:
                    pass
                continue
            try:
                await self._execute(run, stop)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("Webhook run failed run=%s", run["id"])
                self.store.fail_webhook_run(str(run["id"]), type(error).__name__)

    async def _execute(self, run: dict[str, Any], stop: asyncio.Event) -> None:
        run_id = str(run["id"])
        steps = run["plan"]["steps"]
        for index in range(int(run["current_step"]), len(steps)):
            step = steps[index]
            record = self.store.webhook_step(run_id, index)
            if record is None:
                raise RuntimeError("webhook step missing")
            if record["state"] == "succeeded":
                continue
            if step["uses"] == "message":
                if record["state"] != "waiting_delivery":
                    self.store.start_webhook_step(run_id, index)
                    turn_id = f"webhook:{run_id}:{index}"
                    reply = await self.complete_turn(str(step["prompt"]), turn_id)
                    outbox_ids = self.store.commit_webhook_reply(
                        run_id, index, turn_id, reply, self.primary_channel
                    )
                    if not outbox_ids:
                        continue
                    self.wake_outbox()
                if not await self._wait_delivery(run_id, index, stop):
                    return
            else:
                self.store.start_webhook_step(run_id, index)
                logger.debug(
                    "Executing internal workflow executor run=%s name=%s",
                    run_id,
                    step["executor"],
                )
                state, result, error = await self._run_exec(step)
                self.store.finish_webhook_step(run_id, index, state, result, error)
                logger.debug(
                    "Internal workflow executor completed run=%s name=%s state=%s",
                    run_id,
                    step["executor"],
                    state,
                )
                if state != "succeeded":
                    return
        self.store.complete_webhook_run(run_id)

    async def _wait_delivery(
        self, run_id: str, step_index: int, stop: asyncio.Event
    ) -> bool:
        while not stop.is_set():
            state = self.store.webhook_delivery_state(run_id, step_index)
            if state == "succeeded":
                self.store.finish_webhook_step(
                    run_id, step_index, "succeeded", {}, None
                )
                return True
            if state == "failed":
                self.store.finish_webhook_step(
                    run_id, step_index, "failed", {}, "message_delivery_failed"
                )
                return False
            await asyncio.sleep(0.5)
        raise asyncio.CancelledError

    async def _run_exec(
        self, step: dict[str, Any]
    ) -> tuple[str, dict[str, Any], str | None]:
        env = {**os.environ, **step["env"]}
        try:
            process = await asyncio.create_subprocess_exec(
                *step["argv"],
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            return "failed", {}, type(error).__name__
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=float(step["timeout_seconds"])
            )
        except TimeoutError:
            await self._terminate(process)
            return "ambiguous", {}, "executor_timeout"
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        result = {
            "exit_code": process.returncode,
            "stdout_tail": stdout[-16384:].decode(errors="replace"),
            "stderr_tail": stderr[-16384:].decode(errors="replace"),
        }
        if process.returncode == 0:
            return "succeeded", result, None
        return "failed", result, f"executor_exit_{process.returncode}"

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=5)
        except (ProcessLookupError, TimeoutError):
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
