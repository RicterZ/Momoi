import assert from "node:assert/strict";
import test from "node:test";
import { runtimeFieldValue, runtimeFieldChanges } from "../src/runtimeFields.js";

const spec = { properties: {
  owner: { default: "" },
  heartbeat: { default: "" },
  reflection: { default: "" },
} };

test("fills missing stage defaults while retaining configured efforts", () => {
  assert.deepEqual(runtimeFieldValue(spec, { owner: "high" }), { owner: "high", heartbeat: "", reflection: "" });
});

test("only submits changed stages, including an explicit empty string to reset", () => {
  const saved = runtimeFieldValue(spec, { owner: "high", heartbeat: "low" });
  assert.equal(runtimeFieldChanges(spec, { ...saved }, saved), undefined);
  assert.deepEqual(runtimeFieldChanges(spec, { ...saved, owner: "", reflection: "max" }, saved), { owner: "", reflection: "max" });
});

test("handles recursively nested schema and unchanged object values", () => {
  const nested = { properties: { stages: spec } };
  const saved = runtimeFieldValue(nested, {});
  assert.deepEqual(runtimeFieldChanges(nested, { stages: { ...saved.stages, owner: "xhigh" } }, saved), { stages: { owner: "xhigh" } });
  assert.equal(runtimeFieldChanges({ type: "object" }, { value: 1 }, { value: 1 }), undefined);
});

test("topic selection defaults to low but explicit follow-model survives edits", () => {
  const stages = { properties: { topic_selection: { default: "low" }, owner: { default: "" } } };
  const saved = runtimeFieldValue(stages, {});
  assert.equal(saved.topic_selection, "low");
  assert.equal(runtimeFieldValue(stages, { topic_selection: "" }).topic_selection, "");
  assert.deepEqual(runtimeFieldChanges(stages, { ...saved, topic_selection: "medium" }, saved), { topic_selection: "medium" });
  assert.deepEqual(runtimeFieldChanges(stages, { ...saved, topic_selection: "" }, saved), { topic_selection: "" });
});
