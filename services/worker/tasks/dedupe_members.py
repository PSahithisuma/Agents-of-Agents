"""dedupe_members.py -- remove fields/methods that the model wrote twice in one Java class.

Standard library only. Keeps the FIRST declaration of each member and removes later copies together
with the annotations directly above them. Code without duplicates is returned unchanged (idempotent).
Members whose braces do not balance (truncated output) are never touched.
"""
import re

_DQ = re.compile(r'"(?:\\.|[^"\\])*"')
_SQ = re.compile(r"'(?:\\.|[^'\\])*'")
_COMMENT = re.compile(r"//.*$")
_ANN = re.compile(r"^@\w+(\([^)]*\))?$")
_FIELD = re.compile(r"^(?:(?:public|private|protected|static|final|volatile|transient)\s+)*"
                    r"[\w<>\[\],.? ]+?\s+(\w+)\s*(?:=[^;]*)?;$")


def _clean(line):
    """Line with strings and // comments blanked, so braces inside them are not counted."""
    return _COMMENT.sub("", _SQ.sub("''", _DQ.sub('""', line)))


def _net(cleaned_line):
    return cleaned_line.count("{") - cleaned_line.count("}")


def _param_types(params):
    parts, depth, cur = [], 0, ""
    for ch in params:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    types = []
    for p in parts:
        p = re.sub(r"@\w+(\([^)]*\))?\s*", "", p).replace("final ", "").strip()
        toks = p.rsplit(None, 1)
        types.append(re.sub(r"\s+", "", toks[0]) if len(toks) == 2 else p)
    return tuple(types)


def _key(decl):
    d = decl.strip()
    m = _FIELD.match(d)
    if m:
        return ("field", m.group(1))
    if "(" in d and "{" in d and d.count("(") == d.count(")"):
        m = re.search(r"(\w+)\s*\(([^)]*)\)", d)
        if m:
            return ("method", m.group(1), _param_types(m.group(2)))
    return None


def _members(lines):
    """[(start, end, key)] for the top-level members of the first class (indexes inclusive)."""
    cleaned = [_clean(x) for x in lines]
    n = len(lines)
    opener = next((i for i, c in enumerate(cleaned)
                   if re.search(r"\b(class|interface|enum)\s+\w+", c) and "{" in c), None)
    if opener is None:
        return []
    depth = _net(cleaned[opener])
    members, j = [], opener + 1
    while j < n and depth >= 1:
        s = cleaned[j].strip()
        if depth == 1 and s and not s.startswith(("*", "/*", "}", "//")):
            start = k = j
            while _ANN.match(cleaned[k].strip()) and k + 1 < n:
                k += 1
            e, dd = k, depth + _net(cleaned[k])
            while dd > depth and e + 1 < n:
                e += 1
                dd += _net(cleaned[e])
            score = sum(len(re.findall(r"@\w+", cleaned[x])) for x in range(start, k + 1))
            members.append((start, e, _key(cleaned[k]) if dd == depth else None, score))
            j = e + 1
            continue
        depth += _net(cleaned[j])
        j += 1
    return members


def find_duplicates(code):
    seen, dups = set(), []
    lines = code.split("\n")
    for start, end, key, _score in _members(lines):
        if key is None:
            continue
        if key in seen:
            dups.append("%s %s (line %d)" % (key[0], key[1], start + 1))
        seen.add(key)
    return dups


def dedupe_class_members(code):
    """Keep the copy with the most annotations (e.g. the one carrying @PreUpdate); on a tie keep the first."""
    lines = code.split("\n")
    groups = {}
    for start, end, key, score in _members(lines):
        if key is not None:
            groups.setdefault(key, []).append((start, end, score))
    drop = set()
    for items in groups.values():
        if len(items) < 2:
            continue
        keep = max(range(len(items)), key=lambda i: (items[i][2], -i))
        for i, (start, end, _s) in enumerate(items):
            if i != keep:
                drop.update(range(start, end + 1))
                if start > 0 and not lines[start - 1].strip():
                    drop.add(start - 1)
    if not drop:
        return code
    return "\n".join(l for i, l in enumerate(lines) if i not in drop)


if __name__ == "__main__":      # self-test: proves the copy on the VM is intact
    src = ("public class P {\n    @PreUpdate\n    void onUpdate() {\n        x();\n    }\n\n"
           "    @PreUpdate\n    void onUpdate() {\n        x();\n    }\n}\n")
    out = dedupe_class_members(src)
    assert out.count("onUpdate") == 1 and out.count("@PreUpdate") == 1, out
    assert out.count("{") == out.count("}") and dedupe_class_members(out) == out
    print("dedupe_members self-test OK")
