#!/usr/bin/env python3
"""ctclean — turn raw Cheat Engine tables into a WeMod-style cheat list.

Original `.ct` files are never modified. For each game we write one sidecar:

    ~/.local/share/ctfetch/clean/<Game>.json
    {"game": ..., "source_files": [...], "cheats": [
        {"description": "Unlimited Ammo", "category": "Weapons",
         "kind": "script"|"value"|"pointer", "type": "Auto Assembler Script"|"4 Bytes",
         "address": "cheat4_store1", "offsets": [], "script_lang": "aa"|"lua"|"",
         "toggle": true,          # suitable for an on/off switch
         "confidence": "high"|"medium"|"low",
         "source_file": "....CT", "entry_id": "42"}
    ]}

Why the mess: a raw table is a debug view, not a product. Typical junk is
struct dumps (`RagdollScale` ×1373), hex prefixes (`[03C] BypassesShield`),
`GroupHeader` rows that are categories rather than cheats, empty descriptions and
duplicated rows across sub-tables. Some tables are also non-UTF-8 (GBK/Shift-JIS
tables) — descriptions then arrive as mojibake unless the bytes are decoded with
the right codec first.

Confidence, never a hard delete: everything is kept; the UI decides what to show.
  high   — carries an Auto Assembler script, or a short human-looking value name
  medium — matches a cheat keyword (English or CJK) but has no script
  low    — everything else; mostly struct fields, hidden behind "show raw"

CLI:
    python3 ctclean.py                 # rebuild every game's sidecar
    python3 ctclean.py --game "Far Cry 5"
    python3 ctclean.py --stats         # report over the built sidecars
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

TABLES = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "ctfetch" / "tables"
CLEAN = TABLES.parent / "clean"

# Cheat-ish vocabulary. CJK is not optional: the Far Cry / Ubisoft tables are
# Chinese-only ("生命值不减" = health never decreases), and a Latin-only keyword
# pass marks every row in them as junk.
KEYWORDS_EN = re.compile(
    r"infin|unlimit|god|one.?hit|one.?shot|no.?reload|no.?recoil|no.?spread|"
    r"rapid.?fire|super.?speed|speed|money|cash|credit|ammo|clip|magazine|"
    r"health|hp|mana|stamina|oxygen|energy|armor|shield|damage|xp|experience|"
    r"level|gold|resource|inventory|carry|weight|stealth|invisib|freeze|timer|"
    r"time|jump|fly|noclip|teleport|coordinate|position|instant|multiplier|"
    r"rate|lock|max|refill|restore|undetect|detect|hack|inject",
    re.I)
KEYWORDS_CJK = re.compile(
    "无限|无敌|不减|不減|不减|无需|無需|不用|加强|強化|速度|金钱|金錢|钱|"
    "弹药|彈藥|子弹|生命|血量|体力|體力|耐力|氧气|氧氣|能量|护甲|護甲|"
    "伤害|傷害|经验|經驗|等级|等級|资源|資源|物品|背包|负重|負重|潜行|潛行|"
    "隐身|隱身|冻结|凍結|时间|時間|跳跃|跳躍|飞行|飛行|传送|傳送|坐标|座標|"
    "位置|秒杀|秒殺|一击|一擊|探测|探測|透视|透視|最高|增加|修改|不减|"
    "体力值|耐力值|氧气值")

# Names that are struct fields rather than player-facing cheats.
STRUCTISH = re.compile(
    r"^(b[A-Z]\w*|m_?\w+|f[A-Z]\w*|i[A-Z]\w*|[A-Za-z]\w*(Id|Index|Flags?|"
    r"Scale|Chance|Duration|Modifier|Multipliers?|Factor|Rate|Value|Ptr|Pointer|"
    r"Count|Size|Type|Class|Name|State|Mode|Radius|Speed|Grade|Pct|Bonus|"
    r"Interval|Intensity|Bias|Length|Pitch|Yaw|Pos|Position|Max|Min|Num|Number))$")
STRUCT_PATH = re.compile(r"/Script/|^ScriptStruct\b|^Class\s|^Key:\s|^0x[0-9A-Fa-f]+$")


def looks_struct(name: str) -> bool:
    """True for engine/struct field names that mean nothing to a player.

    Three tells, all cheap: a type-suffix camelCase name (`RagdollScale`,
    `SurfaceLightMaxIntensity`), an UnrealScript path (`Class /Script/Engine...`),
    or a bare identifier that starts lowercase or is unusually long
    (`armordamagemultiplier`, `fDesiredMinionMaxMana`).
    """
    compact = name.replace(" ", "")
    if STRUCTISH.match(compact) or STRUCT_PATH.search(name):
        return True
    if " " not in name and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        if name[0].islower() or "_" in name or len(name) > 14:
            return True
    return False
GROUP_HEADER = "GroupHeader"


# ---------------------------------------------------------------- reading
def _prep_bytes(path: Path) -> bytes:
    """Bytes for ElementTree, repairing encodings tables are occasionally saved in.

    ElementTree honours the XML declaration; when it lies (GBK bytes declared as
    utf-8, or no declaration at all) the parse dies, so retry with the codecs
    these tables actually use and hand back a utf-8 document.
    """
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw
    try:
        ET.fromstring(raw)
        return raw
    except ET.ParseError:
        pass
    for enc in ("utf-8", "gb18030", "shift_jis", "cp1252"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        text = re.sub(r'encoding\s*=\s*"[^"]*"', 'encoding="utf-8"', text, count=1)
        return text.encode("utf-8")
    return raw.decode("utf-8", "replace").encode("utf-8")


def _text(el: ET.Element, name: str) -> str:
    child = el.find(name)
    return (child.text or "").strip() if child is not None else ""


def clean_text(desc: str, fallback: str = "") -> str:
    """Human-facing label from a raw Description."""
    d = html.unescape(desc or "")
    d = d.strip().strip('"').strip()
    d = re.sub(r"^x?\s*\[[0-9A-Fa-f]{1,8}\]\s*", "", d)      # [03C] BypassesShield
    d = re.sub(r"^#\d+\s*", "", d)                          # #012
    d = re.sub(r"\s*[-=]>+\s*$", "", d)                     # Item ID -->
    d = re.sub(r"\[[A-Za-z0-9_]+\]$", "", d).strip()        # Damage [CharacterModel]
    d = re.sub(r"\s+", " ", d)
    if not d or d.lower() in ("no description", "description", "n/a", "-"):
        return fallback
    if re.fullmatch(r"[\d\W_]+", d):                        # "[1]", "3", "--"
        return fallback
    return d


def script_lang(script: str | None) -> str:
    if not script:
        return ""
    if "{$lua}" in script or "{$LUA}" in script:
        return "lua"
    if "{$asm}" in script or re.search(r"\b(aobscanmodule|alloc|label|registersymbol)\b", script):
        return "aa"
    return "aa"      # CE defaults to AA syntax


def classify(entry: ET.Element) -> tuple[str, str, str]:
    """(kind, variable_type, script_language)"""
    script = _text(entry, "AssemblerScript") or None
    vtype = _text(entry, "VariableType")
    if script:
        return "script", vtype or "Auto Assembler Script", script_lang(script)
    if vtype and vtype.lower() not in ("string", "binary", "byte array"):
        return "value", vtype, ""
    return "pointer" if _text(entry, "Address") else "pointer", vtype, ""


def is_one_shot(name: str) -> bool:
    """Looks like an action ("Refill Health") rather than a standing toggle."""
    return bool(re.search(r"refill|restore|reset|max(?:imise|imize)? ?(?:all|everything)|"
                          r"give|add|spawn|unlock|teleport|set |修復|恢复|恢復|補充|补充",
                          name, re.I))


# ---------------------------------------------------------------- per file
def parse_file(path: Path) -> list[dict]:
    """Flatten one table into candidate cheat rows (categories from group headers)."""
    rows: list[dict] = []
    try:
        root = ET.fromstring(_prep_bytes(path))
    except ET.ParseError as exc:
        print(f"  ! {path.name}: {exc}", file=sys.stderr)
        return rows

    def walk(el: ET.Element, category: str) -> None:
        for entry in el.findall("CheatEntries/CheatEntry"):
            desc_raw = _text(entry, "Description")
            is_group = _text(entry, GROUP_HEADER) not in ("", "0")
            name = clean_text(desc_raw)
            if is_group:
                walk(entry, name or category)
                continue
            kind, vtype, lang = classify(entry)
            script = _text(entry, "AssemblerScript") or None
            addr = _text(entry, "Address") or None
            offsets = [(o.text or "").strip() for o in entry.findall("./Offsets/Offset")
                       if o is not None and (o.text or "").strip()]
            if name:
                rows.append({
                    "description": name,
                    "category": category or "General",
                    "kind": kind,
                    "type": vtype,
                    "address": addr,
                    "offsets": offsets,
                    "script_lang": lang,
                    "script": script,
                    "entry_id": _text(entry, "ID"),
                    "one_shot": is_one_shot(name),
                    "source_file": path.name,
                })
            walk(entry, category)       # nested group without GroupHeader

    walk(root, "")
    return rows


# ---------------------------------------------------------------- confidence
def score_rows(rows: list[dict]) -> None:
    """Assign confidence in place; repeats across a file are the struct-dump tell.

    Rule of thumb: a script is a cheat by definition. Otherwise a player-facing
    label is either a phrase with intent ("Current Health", "武器无后座") or a
    single word that names a resource ("Money"). Struct fields are single
    identifiers with engine-shaped names and repeat across sub-tables.
    """
    repeats = Counter(r["description"].lower() for r in rows)
    modifier = re.compile(
        r"infin|unlimit|god|one.?hit|one.?shot|no.?reload|no.?recoil|no.?spread|"
        r"double|triple|max|super|instant|refill|restore|free|999|无限|无敌|不减|"
        r"無敵|不減|无后座|无扩散|一击|秒杀|最高|无需|一键|修改", re.I)
    for r in rows:
        name = r["description"]
        keyword = bool(KEYWORDS_EN.search(name) or KEYWORDS_CJK.search(name))
        struct = looks_struct(name)
        dup = repeats[name.lower()]
        if r["kind"] == "script":
            r["confidence"] = "high"
            r["toggle"] = r["script_lang"] == "aa"      # CE Lua cannot run yet
        elif struct:
            r["confidence"] = "low"
            r["toggle"] = False
        elif modifier.search(name) or keyword:
            r["confidence"] = "medium"
            r["toggle"] = r["kind"] == "value" and (r["type"] or "").lower() in ("byte", "2 bytes")
        elif dup >= 5 or len(name) < 3:
            r["confidence"] = "low"
            r["toggle"] = False
        else:
            r["confidence"] = "medium" if r["kind"] == "value" else "low"
            r["toggle"] = False
        if r["one_shot"]:
            r["toggle"] = False


def dedupe(rows: list[dict]) -> list[dict]:
    """One row per (category, description); keep every address we found."""
    seen: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["category"], r["description"])
        if key not in seen:
            r["addresses"] = [r["address"]] if r["address"] else []
            seen[key] = r
        else:
            if r["address"] and r["address"] not in seen[key]["addresses"]:
                seen[key]["addresses"].append(r["address"])
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(seen.values(), key=lambda r: (order[r["confidence"]], r["category"], r["description"].lower()))


# ---------------------------------------------------------------- one game
def clean_game(game_dir: Path) -> dict:
    rows: list[dict] = []
    files = []
    for path in sorted(p for p in game_dir.iterdir() if p.suffix.lower() == ".ct"):
        files.append(path.name)
        rows.extend(parse_file(path))
    score_rows(rows)
    cheats = dedupe(rows)
    return {"game": game_dir.name, "source_files": files, "cheats": cheats}


def write_game(game_dir: Path) -> dict:
    data = clean_game(game_dir)
    CLEAN.mkdir(parents=True, exist_ok=True)
    out = CLEAN / f"{game_dir.name}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def load_clean(game: str, generate: bool = True) -> dict:
    """Sidecar for one game, built on demand if it is missing."""
    path = CLEAN / f"{game}.json"
    if not path.exists() and generate:
        game_dir = TABLES / game
        if game_dir.is_dir():
            return write_game(game_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"game": game, "cheats": []}


def stats() -> None:
    total = conf = Counter()
    games = 0
    for f in sorted(CLEAN.glob("*.json")):
        games += 1
        data = json.loads(f.read_text(encoding="utf-8"))
        for c in data["cheats"]:
            total["cheats"] += 1
            conf[c["confidence"]] += 1
            total["toggle"] += bool(c.get("toggle"))
    print(f"games {games}  cheats {total['cheats']}  toggles {total['toggle']}")
    for k in ("high", "medium", "low"):
        share = conf[k] / max(1, total["cheats"])
        print(f"  {k:6} {conf[k]:6}  {share:5.1%}")


def main() -> None:
    args = sys.argv[1:]
    if "--stats" in args:
        stats()
        return
    if "--game" in args:
        name = args[args.index("--game") + 1]
        game_dir = TABLES / name
        if not game_dir.is_dir():
            sys.exit(f"no such game dir: {game_dir}")
        data = write_game(game_dir)
        hi = sum(1 for c in data["cheats"] if c["confidence"] == "high")
        print(f"{name}: {len(data['cheats'])} cheats ({hi} high) -> {CLEAN / (name + '.json')}")
        return
    games = sorted(p for p in TABLES.iterdir() if p.is_dir()) if TABLES.exists() else []
    done = 0
    for game_dir in games:
        if not any(p.suffix.lower() == ".ct" for p in game_dir.iterdir()):
            continue
        data = write_game(game_dir)
        done += 1
        hi = sum(1 for c in data["cheats"] if c["confidence"] == "high")
        print(f"{game_dir.name[:44]:44} {len(data['cheats']):4} cheats  {hi:4} high")
    print(f"\nwrote {done} sidecars to {CLEAN}")
    stats()


if __name__ == "__main__":
    main()
