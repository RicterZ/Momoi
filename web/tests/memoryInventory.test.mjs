import assert from "node:assert/strict";
import test from "node:test";
import { filterMemories, memoryScopeLabel } from "../src/memoryInventory.js";

const rows = [
  { id: 1, activation: "recall", content: "想找小桃聊天", meta: { scope: "", tags: ["communication"], triggers: ["喵", "FF7"] } },
  { id: 2, activation: "scoped", content: "先看是否喝过水", scope_label: "Goal · 喝水", meta: { scope: "goal:one", tags: ["health"] } },
  { id: 3, activation: "scoped", content: "扫地任务通知", meta: { scope: "webhook", triggers: [] } },
  { id: 4, activation: "reflection", content: "今天的复盘" },
];

test("combines scope, activation and trigger search without including other scopes", () => {
  assert.deepEqual(filterMemories(rows, { scope: "", query: "喵", activation: "recall" }).map(x => x.id), [1]);
  assert.deepEqual(filterMemories(rows, { scope: "webhook", query: "喵" }), []);
  assert.deepEqual(filterMemories(rows, { scope: "goal:one" }).map(x => x.id), [2]);
  assert.deepEqual(filterMemories(rows, { scope: "" }).map(x => x.id), [1, 4]);
});

test("search matches normalized triggers, tag labels and scope titles", () => {
  for (const query of ["ｆｆ７", "交流互动", "communication"]) {
    assert.deepEqual(filterMemories(rows, { query }).map(x => x.id), [1]);
  }
  assert.deepEqual(filterMemories(rows, { query: "Goal · 喝水" }).map(x => x.id), [2]);
  assert.equal(memoryScopeLabel(rows[2]), "webhook");
  assert.equal(memoryScopeLabel(rows[0]), "全局");
  assert.deepEqual(filterMemories(rows, { query: "  " }), rows);
});
