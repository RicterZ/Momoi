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

test("groups by primary tag, sorts by usage and keeps untagged last", async () => {
  const { groupMemories, memoryTagCounts, UNTAGGED } = await import("../src/memoryInventory.js");
  const items = [
    { id: 1, updated_at: 10, meta: { tags: ["health", "food_drink"] } },
    { id: 2, updated_at: 20, meta: { tags: ["health"] }, access: { counts: { recalled: 3, searched: 1 } } },
    { id: 3, updated_at: 30, meta: { tags: [] } },
    { id: 4, updated_at: 40, meta: { tags: ["food_drink"] } },
    { id: 5, updated_at: 50, meta: { tags: ["health"] } },
  ];
  const groups = groupMemories(items);
  assert.deepEqual(groups.map(g => [g.key, g.items.map(x => x.id)]), [["health", [2, 5, 1]], ["food_drink", [4]], [UNTAGGED, [3]]]);
  assert.deepEqual(groupMemories(items, { sort: "updated" })[0].items.map(x => x.id), [5, 2, 1]);
  assert.deepEqual(memoryTagCounts(items).map(x => [x.tag, x.count]), [["health", 3], ["food_drink", 2], [UNTAGGED, 1]]);
  assert.deepEqual(filterMemories(items, { tags: [UNTAGGED, "food_drink"] }).map(x => x.id), [1, 3, 4]);
  assert.deepEqual(groupMemories(rows.slice(1, 3), { by: "scope" }).map(g => g.label), ["Goal · 喝水", "webhook"]);
});
