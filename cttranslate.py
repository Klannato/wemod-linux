#!/usr/bin/env python3
"""cttranslate — English labels for non-English cheat names, offline.

Only ~350 unique non-ASCII strings exist across the whole library (most tables
are English, and 215 of those strings are already bilingual like
"Act pt. / 行動力"). So translation is a one-time, cacheable pass, not a service:

  1. **Bilingual split** — when a name carries " / ", keep the English half.
  2. **Pattern + glossary** — cheat vocabulary is tiny and formulaic
     ("弹匣不减" = magazine + not-decreasing), so a curated term map plus a few
     patterns covers almost everything deterministically.
  3. **LLM (optional, `--llm`)** — only for what steps 1–2 cannot resolve, using
     the same free OpenRouter key as the agent. Off by default: nothing here
     needs the network.

Results are cached in `~/.local/share/ctfetch/clean/translations.json` keyed by
the original string, so a manual correction is never lost and never re-run.

CLI:
    python3 cttranslate.py --stats        # coverage without writing anything
    python3 cttranslate.py --apply        # add description_en to every sidecar
    python3 cttranslate.py --review       # list what stayed untranslated
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import ctclean as CL

CACHE = CL.CLEAN / "translations.json"
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")

# Cheat vocabulary seen in the corpus. Longest keys win, so order matters at
# match time, not here.
TERMS = {
    "不被探测": "Undetected", "不被侦察": "Undetected by Recon", "不被發現": "Undetected",
    "无限生命值": "Unlimited Health", "生命值": "Health", "血量": "Health",
    "耐力值": "Stamina", "氧气值": "Oxygen", "体力": "Stamina", "氧气": "Oxygen",
    "弹匣": "Magazine", "备弹": "Reserve Ammo", "弹药": "Ammo", "子弹": "Bullets",
    "火药": "Powder", "武器": "Weapon", "后座": "Recoil", "扩散": "Spread",
    "金钱": "Money", "现金": "Cash", "特长点数": "Perk Points", "技能点": "Skill Points",
    "经验值": "EXP", "经验": "EXP", "等级": "Level", "资源": "Resources",
    "坐标": "Coordinate", "标记": "Marker", "人物": "Player", "角色": "Character",
    "传送": "Teleport", "执行传送": "Teleport", "针剂": "Syringe", "钓鱼": "Fishing",
    "冲刺": "Sprint", "游泳": "Swim", "站立": "Standing", "蹲下": "Crouch",
    "移动速度": "Move Speed", "速度": "Speed", "跳跃": "Jump", "飞行": "Fly",
    "重力": "Gravity", "生命": "Health", "护甲": "Armor", "伤害": "Damage",
    "注入": "Inject", "代码": "Code", "地址": "Address", "数量": "Amount",
    "当前值": "Current Value", "初始": "Initial", "自动": "Auto", "全部": "All",
    "任务": "Mission", "时间": "Time", "锁定": "Freeze", "冻结": "Freeze",
    "无敌": "God Mode", "秒杀": "One Hit Kill", "一击必杀": "One Hit Kill",
    "每種针剂": "each syringe", "每种针剂": "each syringe", "不断线": "Line Never Breaks",
    "获取": "Get", "设置": "Set", "用于": "for", "修改": "Modify", "开启": "On",
    "关闭": "Off", "個": "", "个": "", "发": "rounds",
    # second batch, from the unresolved review pass
    "一击秒杀": "One Hit Kill", "一键开图": "Reveal Map (one click)",
    "不受伤害": "No Damage", "专注值": "Focus", "卡牌次数": "Card Uses",
    "卡牌立刻冷却": "Instant Card Cooldown", "取消敌方护盾": "Remove Enemy Shields",
    "基础伤害": "Base Damage", "备用弹药": "Reserve Ammo", "仅显示": "display only",
    "失败任务数量": "Failed Mission Count", "己方": "Allies", "敌方": "Enemies",
    "当前弹药": "Current Ammo", "当前经验": "Current EXP", "当前警员": "Current Officer",
    "能力值": "Stats", "探员槽位": "Agent Slot", "处理模式": "Handling Mode",
    "燃料": "Fuel", "空格技能": "Empty Slot Skills", "符文卡牌": "Rune Cards",
    "魔力": "Mana", "法力值": "Mana", "换弹": "Reload", "最大值": "Max Value",
    "梯子": "Ladders", "路灯": "Streetlights", "射速": "Fire Rate",
    "精准度": "Accuracy", "注入方法": "Injection Method", "喷射能量": "Jetpack Energy",
    "车辆": "Vehicles", "武器弹药": "Weapon Ammo", "冷却": "Cooldown",
    "和": "and", "增强": "Boost", "不增加": "Never Increases", "不减少": "Never Decreases",
    "免疫": "Immune to", "攻击": "Attacks", "角色": "Character", "箭": "Arrows",
    "地雷": "Mines", "指针": "Pointer", "警员": "Officer", "状态": "Status",
    "提升": "Boost", "道具": "Items", "倍": "x", "值": "", "鼠标": "Mouse",
    "滑动": "Swipe", "制作材料": "Crafting Materials", "升级物品": "Upgrade Items",
    "其它资源": "Other Resources", "注射器": "Syringes", "手雷": "Grenades",
    "反增": "Only Increases", "无需": "No", "最初に": "first", "开图": "Reveal Map",
    "能量": "Energy", "火星": "Mars", "槽位": "Slot", "金币": "Money",
    "标记点": "Marker", "到": "to", "点": "",
    "アイテム": "Item", "減少しない": "Never Decreases", "回復": "Restore",
    "注意": "Note", "お読みください": "read this first", "スイッチ": "Switch",
}

# Formulaic shapes: regex first, term substitution second.
PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(.+?)不减$"), r"Unlimited \1"),
    (re.compile(r"^无限(.+)$"), r"Unlimited \1"),
    (re.compile(r"^(.+?)无后座$"), r"No Recoil"),
    (re.compile(r"^(.+?)无扩散$"), r"No Spread"),
    (re.compile(r"^(.+?)无限$"), r"Unlimited \1"),
    (re.compile(r"^获取(.+?)地址$"), r"\1 Address"),
    (re.compile(r"^获取(.+)$"), r"Get \1"),
    (re.compile(r"^设置(.+)$"), r"Set \1"),
    (re.compile(r"^(.+?)\s*([XYZ])\s*坐标$"), r"\1 \2 Coordinate"),
    (re.compile(r"^(.+?)\s*坐标$"), r"\1 Coordinate"),
    (re.compile(r"^(.+?)\s*(\d+)\s*发$"), r"\1 \2 rounds"),
    (re.compile(r"^两倍(.+)$"), r"2× \1"),
    (re.compile(r"^三倍(.+)$"), r"3× \1"),
    (re.compile(r"^二倍(.+)$"), r"2× \1"),
    (re.compile(r"^(.+?)（火星DLC）$"), r"\1 (Mars DLC)"),
    (re.compile(r"^(.+?)\(火星DLC\)$"), r"\1 (Mars DLC)"),
    (re.compile(r"^(.+?)（Ctrl\+(\w+)）$", re.I), r"\1 (Ctrl+\2)"),
    (re.compile(r"^(.+?)\s*\(([^)]*[A-Za-z][^)]*)\)$"), r"\1 (\2)"),
]


def _ascii_ratio(s: str) -> float:
    if not s:
        return 0.0
    return sum(1 for ch in s if ord(ch) < 128) / len(s)


def split_bilingual(name: str) -> str | None:
    """Keep the English half of 'English / 日本語' style names."""
    if " / " not in name:
        return None
    parts = [p.strip() for p in name.split(" / ")]
    latin = [p for p in parts if _ascii_ratio(p) > 0.75 and re.search(r"[A-Za-z]{2}", p)]
    if not latin:
        return None
    best = max(latin, key=len)
    return re.sub(r"\s+", " ", best).strip(" -–—:") or None


PAREN = re.compile(r"^(.*?)\s*[（(]([^）)]*)[）)]\s*$")


def glossary(name: str) -> str | None:
    """Pattern + term substitution; None when the result is still not English.

    Order matters: peel a trailing parenthetical first (otherwise
    "喷射能量不减（火星DLC）" never matches the "…不减" shape), then run the
    formulaic patterns, then substitute vocabulary.
    """
    suffix = ""
    m = PAREN.match(name)
    if m:
        name, suffix = m.group(1).strip(), m.group(2).strip()

    for pat, repl in PATTERNS:
        m = pat.match(name)
        if m:
            name = m.expand(repl) if m.groups() else repl
            break
    if TERMS and CJK.search(name):
        for term in sorted(TERMS, key=len, reverse=True):
            if term in name:
                name = name.replace(term, f" {TERMS[term]} ")
    name = name.replace("、", ", ").replace("，", ", ")
    name = re.sub(r"\s+([,.)])", r"\1", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" 、,:-")
    if suffix:
        if CJK.search(suffix):
            for term in sorted(TERMS, key=len, reverse=True):
                if term in suffix:
                    suffix = suffix.replace(term, f" {TERMS[term]} ")
            suffix = re.sub(r"\s{2,}", " ", suffix).strip()
            if CJK.search(suffix):
                suffix = ""
        if suffix:
            name = f"{name} ({suffix})" if name else suffix
    if not name or CJK.search(name) or _ascii_ratio(name) < 0.75:
        return None
    name = re.sub(r"\s{2,}", " ", name).strip()
    return name[0].upper() + name[1:]


NAME_ONLY = re.compile(r"^[\u4e00-\u9fff]{2,4}$")


def translate(name: str) -> tuple[str, str] | None:
    """(english, method) or None."""
    if not CJK.search(name):
        return None
    got = split_bilingual(name)
    if got:
        return got, "bilingual"
    got = glossary(name)
    if got:
        return got, "glossary"
    if NAME_ONLY.match(name.strip()) and not any(t in name for t in TERMS):
        # 2-4 Han characters with no cheat vocabulary is a character/NPC name
        # (上官螢, 尹志平 …). Translating a name is worse than leaving it.
        return name, "name"
    return None


# ---------------------------------------------------------------- library pass
def collect() -> dict[str, int]:
    seen: dict[str, int] = {}
    for path in sorted(CL.CLEAN.glob("*.json")):
        if path.name == CACHE.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for c in data["cheats"]:
            if c["confidence"] == "low":
                continue
            if CJK.search(c["description"]):
                seen[c["description"]] = seen.get(c["description"], 0) + 1
    return seen


def load_cache() -> dict[str, dict]:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build(use_llm: bool = False) -> dict[str, dict]:
    cache = load_cache()
    todo = collect()
    for original in todo:
        if original in cache and cache[original].get("english"):
            continue
        got = translate(original)
        if got:
            cache[original] = {"english": got[0], "method": got[1]}
        else:
            cache.setdefault(original, {"english": None, "method": "unresolved"})
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return cache


def apply(cache: dict[str, dict]) -> int:
    changed = 0
    for path in sorted(CL.CLEAN.glob("*.json")):
        if path.name == CACHE.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        touched = False
        for c in data["cheats"]:
            entry = cache.get(c["description"])
            if entry and entry.get("english"):
                c["description_en"] = entry["english"]
                c["translator"] = entry["method"]
                touched = True
        if touched:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            changed += 1
    return changed


def report(cache: dict[str, dict]) -> None:
    by_method: dict[str, int] = {}
    for entry in cache.values():
        key = entry.get("method") or "unresolved"
        by_method[key] = by_method.get(key, 0) + 1
    total = len(cache)
    done = total - by_method.get("unresolved", 0)
    print(f"unique non-English names: {total}   translated: {done} "
          f"({done / max(1, total):.0%})")
    for key, n in sorted(by_method.items(), key=lambda kv: -kv[1]):
        print(f"  {key:10} {n}")


def main() -> None:
    args = sys.argv[1:]
    if "--review" in args:
        cache = load_cache()
        left = [k for k, v in cache.items() if not v.get("english")]
        print(f"{len(left)} untranslated")
        for k in sorted(left):
            print(f"  {k}")
        return
    cache = build(use_llm="--llm" in args)
    report(cache)
    if "--apply" in args:
        n = apply(cache)
        print(f"updated {n} sidecars with description_en")
    print(f"cache: {CACHE}")


if __name__ == "__main__":
    main()
