export const typeNames = {
  module: "模组",
  rule: "规则",
  character: "角色卡",
  soul: "Soul",
  skill: "Skill",
  asset: "素材",
};

export function parseJson(value, label) {
  try {
    return JSON.parse(value || "{}");
  } catch {
    throw new Error(`${label} 不是有效 JSON`);
  }
}
