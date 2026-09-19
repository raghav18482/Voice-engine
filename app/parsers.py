"""Deterministic parsers and validators for spoken answers.

Every field value the agent accepts passes through one of these, whether it came from
rules or from the LLM. Speech-to-text gives us words ("zero four one two", "j dot smith
at gmail dot com"), so each parser normalises speech first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

# ---------------------------------------------------------------- spoken numbers
UNITS = {"zero": 0, "oh": 0, "o": 0, "nought": 0, "nil": 0, "one": 1, "two": 2, "three": 3, "four": 4,
         "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
        "eighty": 80, "ninety": 90}
ORD_UNITS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
             "eighth": 8, "ninth": 9}
ORD_OTHER = {"tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
             "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
             "twentieth": 20, "thirtieth": 30}
MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
          "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
          "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12}
MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]
DIGIT_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]


def words_to_digits(text: str, ordinals: bool = False) -> str:
    """'zero four one two double five' -> '0 4 1 2 5 5'; 'twenty first' -> '21'; 'two thousand' -> '2000'."""
    toks = re.findall(r"[a-z]+|\d+|[^\sa-z\d]", text.lower())
    out: list[str] = []

    def is_num(t: Optional[str]) -> bool:
        return t is not None and (t.isdigit() or t in UNITS or t in TEENS or t in TENS)

    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        prev = toks[i - 1] if i > 0 else None
        if t in ("double", "triple") and nxt and (nxt in UNITS or (nxt.isdigit() and len(nxt) == 1)):
            d = str(UNITS[nxt]) if nxt in UNITS else nxt
            out.extend([d] * (2 if t == "double" else 3))
            i += 2
            continue
        if t in ("oh", "o") and not (is_num(prev) or is_num(nxt)):
            out.append(t)  # interjection, not zero
            i += 1
            continue
        if t in TENS:
            val = TENS[t]
            if nxt in UNITS and UNITS[nxt] > 0 and nxt not in ("oh", "o"):
                val += UNITS[nxt]
                i += 1
            elif ordinals and nxt in ORD_UNITS:
                val += ORD_UNITS[nxt]
                i += 1
            out.append(str(val))
            i += 1
            continue
        if t in TEENS:
            out.append(str(TEENS[t]))
            i += 1
            continue
        if t in UNITS:
            out.append(str(UNITS[t]))
            i += 1
            continue
        if ordinals and t in ORD_UNITS:
            out.append(str(ORD_UNITS[t]))
            i += 1
            continue
        if ordinals and t in ORD_OTHER:
            out.append(str(ORD_OTHER[t]))
            i += 1
            continue
        if t in ("hundred", "thousand") and out and out[-1].isdigit():
            mult = 100 if t == "hundred" else 1000
            base = int(out.pop())
            # 'two thousand and twelve' / 'nineteen hundred'
            out.append(str(base * mult))
            i += 1
            continue
        if t == "and" and out and out[-1].isdigit() and int(out[-1]) >= 100 and is_num(nxt):
            # 'two hundred and fifty' -> join
            j = i + 1
            rest = words_to_digits(" ".join(toks[j:j + 2]))
            parts = rest.split()
            if parts and parts[0].isdigit():
                out[-1] = str(int(out[-1]) + int(parts[0]))
                i = j + (2 if nxt in TENS and len(toks) > j + 1 and toks[j + 1] in UNITS else 1)
                continue
        out.append(t)
        i += 1
    s = " ".join(out)
    # 'two thousand 5' style tails: '2000 5' -> keep; 'X00 Y' where Y<100 -> sum
    s = re.sub(r"\b(\d)00 (\d{1,2})\b", lambda m: str(int(m.group(1)) * 100 + int(m.group(2))), s)
    return s


def digits_only(text: str) -> str:
    return "".join(re.findall(r"\d", words_to_digits(text)))


def spell_digits(d: str, groups: tuple[int, ...] = ()) -> str:
    words = [DIGIT_WORDS[int(c)] for c in d]
    if not groups:
        return " ".join(words)
    parts, i = [], 0
    for g in groups:
        parts.append(" ".join(words[i:i + g]))
        i += g
    if i < len(words):
        parts.append(" ".join(words[i:]))
    return ", ".join(p for p in parts if p)


# ---------------------------------------------------------------- result type
@dataclass
class Parsed:
    ok: bool
    value: Any = None
    display: str = ""       # how the console shows it
    spoken: str = ""        # how the agent reads it back
    error: str = ""         # hint for the re-ask
    skip: bool = False
    extra: dict = field(default_factory=dict)


FILLER = re.compile(
    r"^\s*((yeah|yep|yes|sure|ok|okay|so|um+|uh+|ah+|er+|well|right|look|oh)[,.]?\s+)*", re.I)
SKIP_WORDS = re.compile(r"\b(skip|don'?t know|do not know|not sure|no idea|can'?t remember|rather not|pass)\b", re.I)


def strip_filler(text: str) -> str:
    return FILLER.sub("", text).strip(" .,!?")


# ---------------------------------------------------------------- yes / no
AFFIRM = re.compile(r"\b(yes|yeah|yep|yup|ya|yea|sure|correct|right|that'?s right|ok|okay|absolutely|definitely|"
                    r"of course|go ahead|please do|i do|we do|that'?s fine|fine|agree|i agree|affirmative|"
                    r"sounds good|no worries|no problem|alright|all right|spot on|exactly|true|happy to)\b", re.I)
DENY = re.compile(r"\b(no|nope|nah|not|don'?t|do not|negative|never|incorrect|wrong|isn'?t|none|nothing)\b", re.I)
AFFIRM_OVERRIDES = re.compile(r"\b(no worries|no problem|not a problem|don'?t mind|do not mind|no dramas|why not)\b", re.I)


def yes_no(text: str) -> Optional[bool]:
    t = text.lower()
    if AFFIRM_OVERRIDES.search(t):
        t2 = AFFIRM_OVERRIDES.sub(" ", t)
        if not DENY.search(t2):
            return True
    a = bool(AFFIRM.search(t))
    d = bool(DENY.search(t))
    if a and d:
        # "yeah no" (Aussie for no), "that's right, no" -> ambiguous unless it starts clearly
        if re.match(r"^\s*(no|nope|nah)\b", t) and not re.search(r"\b(yes|yeah|yep)\b\s*$", t):
            return False
        if re.search(r"\byeah,? no\b", t):
            return False
        return None
    if a:
        return True
    if d:
        return False
    return None


def parse_yes_no(text: str, spec: dict, ctx: dict) -> Parsed:
    v = yes_no(text)
    if v is None:
        return Parsed(False, error="I just need a yes or no.")
    return Parsed(True, "yes" if v else "no", "Yes" if v else "No", "yes" if v else "no")


# ---------------------------------------------------------------- enum
def parse_enum(text: str, spec: dict, ctx: dict) -> Parsed:
    t = " " + re.sub(r"[^a-z' ]", " ", text.lower()) + " "
    for opt, syns in spec.get("options", {}).items():
        for s in [opt, *syns]:
            if re.search(r"\b" + re.escape(str(s).lower()) + r"\b", t):
                return Parsed(True, opt, str(opt).capitalize(), str(opt))
    return Parsed(False, error="")


# ---------------------------------------------------------------- postcode
def postcode_state(pc: str) -> Optional[str]:
    n = int(pc)
    if 200 <= n <= 299 or 2600 <= n <= 2618 or 2900 <= n <= 2920:
        return "ACT"
    if 800 <= n <= 999:
        return "NT"
    if 1000 <= n <= 2999:
        return "NSW"
    if 3000 <= n <= 3999 or 8000 <= n <= 8999:
        return "VIC"
    if 4000 <= n <= 4999 or 9000 <= n <= 9999:
        return "QLD"
    if 5000 <= n <= 5999:
        return "SA"
    if 6000 <= n <= 6999:
        return "WA"
    if 7000 <= n <= 7999:
        return "TAS"
    return None


def parse_postcode(text: str, spec: dict, ctx: dict) -> Parsed:
    s = words_to_digits(text)
    m = re.findall(r"(?<!\d)(\d{4})(?!\d)", s)
    cand = m[-1] if m else None
    if not cand:
        d = digits_only(text)
        cand = d if len(d) == 4 else None
    if not cand:
        return Parsed(False, error="A postcode has four digits.")
    st = postcode_state(cand)
    if not st:
        return Parsed(False, error="That doesn't look like an Australian postcode.")
    return Parsed(True, cand, f"{cand} ({st})", spell_digits(cand), extra={"state": st})


# ---------------------------------------------------------------- mobile
def parse_mobile(text: str, spec: dict, ctx: dict) -> Parsed:
    d = digits_only(text)
    if d.startswith("61") and len(d) == 11:
        d = "0" + d[2:]
    m = re.search(r"04\d{8}", d)
    if not m:
        return Parsed(False, error="An Australian mobile starts with zero four and has ten digits.")
    d = m.group(0)
    return Parsed(True, d, f"{d[:4]} {d[4:7]} {d[7:]}", spell_digits(d, (4, 3, 3)))


# ---------------------------------------------------------------- email
DOMAIN_FIXES = {"gmial": "gmail", "gmal": "gmail", "gamil": "gmail", "gmali": "gmail", "gnail": "gmail",
                "hotmial": "hotmail", "hotmal": "hotmail", "outlok": "outlook", "outlookk": "outlook",
                "yahooo": "yahoo", "yaho": "yahoo", "iclod": "icloud", "bigpnd": "bigpond"}
TLD_FIXES = {".con": ".com", ".cmo": ".com", ".comm": ".com"}


EMAIL_STOP = {"no", "nope", "nah", "yes", "yeah", "yep", "it's", "its", "it", "is", "that's", "thats", "sorry", "actually",
              "my", "email", "address", "the", "correct", "correction", "should", "be", "not", "wrong", "right",
              "so", "um", "uh", "ok", "okay", "sure", "s", "i", "said", "meant", "was", "a"}


def parse_email(text: str, spec: dict, ctx: dict) -> Parsed:
    t = text.lower()
    t = re.sub(r"^.*?\b(email|e-mail)( address)?( is)?\b[:,]?", " ", t) if re.search(r"\b(email|e-mail)\b", t) else t
    t = re.sub(r"^\s*(it'?s|it is|that'?s|yeah|yes|sure|ok|okay)\b[,]?", " ", t)
    t = re.sub(r"\b(at sign|at symbol|at)\b", "@", t)
    t = re.sub(r"\b(dot|full stop|period|point)\b", ".", t)
    t = re.sub(r"\b(underscore|under score)\b", "_", t)
    t = re.sub(r"\b(dash|hyphen|minus)\b", "-", t)
    t = re.sub(r"\bcapital\b|\bletter\b|\blower ?case\b", " ", t)
    t = words_to_digits(t)
    if "@" in t:
        # keep only the spoken local part: walk back from '@' until a conversational word
        pre, post = t.split("@", 1)
        toks = re.findall(r"[a-z0-9._%+\-]+|'", pre)
        keep = []
        for tok in reversed(toks):
            if tok in EMAIL_STOP or tok == "'":
                break
            keep.append(tok)
        t = "".join(reversed(keep)) + "@" + post
    t = re.sub(r"\s+", "", t).strip(".,")
    m = re.search(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+", t)
    if not m:
        return Parsed(False, error="Could you spell it out, and say 'at' and 'dot' where they go?")
    email = m.group(0).strip(".")
    local, domain = email.split("@", 1)
    fixed = False
    base = domain.split(".")[0]
    if base in DOMAIN_FIXES:
        domain = domain.replace(base, DOMAIN_FIXES[base], 1)
        fixed = True
    if "." not in domain and domain in ("gmail", "hotmail", "outlook", "yahoo", "icloud", "live"):
        domain += ".com"
    if domain == "bigpond":
        domain += ".com"
    for bad, good in TLD_FIXES.items():
        if domain.endswith(bad):
            domain = domain[: -len(bad)] + good
            fixed = True
            break
    email = f"{local}@{domain}"
    if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)*\.[a-z]{2,}", email):
        return Parsed(False, error="That email doesn't look complete. Could you spell it out, with 'at' and 'dot'?")
    spoken = email.replace(".", " dot ").replace("@", " at ").replace("_", " underscore ").replace("-", " dash ")
    return Parsed(True, email, email, re.sub(r"\s+", " ", spoken).strip(), extra={"typo_fixed": fixed})


# ---------------------------------------------------------------- names
NAME_STOP = {"my", "name", "is", "it's", "its", "i'm", "im", "i", "am", "this", "call", "me", "first", "last",
             "surname", "family", "and", "as", "per", "on", "the", "id", "yes", "yeah", "yep", "sure", "okay",
             "ok", "full", "it", "that's", "thats", "so", "um", "uh", "well", "given", "legal", "licence",
             "license", "passport", "just", "please", "go", "by", "called", "spelt", "spelled", "a", "an"}
TITLES = {"mr": "Mr", "mister": "Mr", "mrs": "Mrs", "missus": "Mrs", "ms": "Ms", "miss": "Miss", "dr": "Dr",
          "doctor": "Dr", "mx": "Mx"}


def parse_full_name(text: str, spec: dict, ctx: dict) -> Parsed:
    toks = re.findall(r"[A-Za-z][A-Za-z'\-]*", text)
    title = None
    words = []
    for w in toks:
        lw = w.lower().strip("'")
        if lw in TITLES:
            title = TITLES[lw]
            continue
        if lw in NAME_STOP:
            continue
        words.append(w[0].upper() + w[1:].lower())
    partial = ctx.get("partial")
    if partial and len(words) == 1:
        words = [partial, words[0]]
    if len(words) >= 2:
        first, last = words[0], words[-1]
        full = f"{first} {last}"
        return Parsed(True, {"first_name": first, "last_name": last, "title": title}, full, full)
    if len(words) == 1:
        return Parsed(False, error="And what's your last name, as it appears on your ID?",
                      extra={"partial": words[0]})
    return Parsed(False, error="")


# ---------------------------------------------------------------- dates
def _clean_date_text(text: str) -> str:
    s = words_to_digits(text, ordinals=True)
    s = re.sub(r"(\d+)\s*(st|nd|rd|th)\b", r"\1", s)
    s = re.sub(r"\b(of|the|on|born|in)\b", " ", s)
    # years spoken as 'nineteen eighty five' -> '19 85' -> '1985'
    s = re.sub(r"\b(19|20)\s+(\d{2})\b", r"\1\2", s)
    s = re.sub(r"\b(\d) (\d) (\d) (\d)\b", r"\1\2\3\4", s)
    return re.sub(r"\s+", " ", s)


def _find_date(s: str) -> Optional[tuple[int, Optional[int], int]]:
    """Returns (day, year|None, month) from cleaned text."""
    m = re.search(r"\b(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{2,4})\b", s)
    if m:
        y = int(m.group(3))
        y = y + (1900 if y > 30 else 2000) if y < 100 else y
        return int(m.group(1)), y, int(m.group(2))
    mon_re = "|".join(sorted(MONTHS, key=len, reverse=True))
    m = re.search(rf"\b(\d{{1,2}})\s*,?\s*({mon_re})\b\s*,?\s*(\d{{4}})?", s)
    if m:
        return int(m.group(1)), int(m.group(3)) if m.group(3) else None, MONTHS[m.group(2)]
    m = re.search(rf"\b({mon_re})\s*,?\s*(\d{{1,2}})\b\s*,?\s*(\d{{4}})?", s)
    if m:
        return int(m.group(2)), int(m.group(3)) if m.group(3) else None, MONTHS[m.group(1)]
    return None


def _fmt_date(d: date) -> tuple[str, str]:
    return d.isoformat(), f"{d.day} {MONTH_NAMES[d.month]} {d.year}"


def parse_dob(text: str, spec: dict, ctx: dict) -> Parsed:
    s = _clean_date_text(text)
    f = _find_date(s)
    if not f or f[1] is None:
        return Parsed(False, error="Please include the day, month and year.")
    day, year, month = f
    try:
        d = date(year, month, day)
    except ValueError:
        return Parsed(False, error="That date doesn't seem right.")
    today = ctx.get("today") or date.today()
    age = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
    if age < 18:
        return Parsed(False, error="The account holder needs to be at least eighteen.")
    if age > 110:
        return Parsed(False, error="That date doesn't seem right.")
    iso, spoken = _fmt_date(d)
    return Parsed(True, iso, spoken, spoken)


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_future_date(text: str, spec: dict, ctx: dict) -> Parsed:
    today = ctx.get("today") or date.today()
    t = text.lower()
    d: Optional[date] = None
    if re.search(r"as soon as possible|asap|straight away|right away|now", t):
        d = today + timedelta(days=2)
    elif "day after tomorrow" in t:
        d = today + timedelta(days=2)
    elif "tomorrow" in t:
        d = today + timedelta(days=1)
    elif "today" in t:
        d = today
    elif re.search(r"next week", t):
        d = today + timedelta(days=7)
    else:
        for i, wd in enumerate(WEEKDAYS):
            if re.search(rf"\b{wd}\b", t):
                delta = (i - today.weekday()) % 7 or 7
                d = today + timedelta(days=delta)
                break
    if d is None:
        f = _find_date(_clean_date_text(text))
        if f:
            day, year, month = f
            try:
                d = date(year or today.year, month, day)
                if year is None and d < today:
                    d = date(today.year + 1, month, day)
            except ValueError:
                return Parsed(False, error="That date doesn't seem right.")
    if d is None:
        return Parsed(False, error="A day and month is fine, like the fifteenth of October.")
    if d < today or (d - today).days > 90:
        return Parsed(False, error="The date needs to be within the next three months.")
    iso, spoken = _fmt_date(d)
    return Parsed(True, iso, spoken, spoken)


# ---------------------------------------------------------------- address
def parse_address(text: str, spec: dict, ctx: dict) -> Parsed:
    t = re.sub(r"^.*?\b(address|it'?s|it is)\b\s*(is)?[:,]?", "", text, count=1, flags=re.I) \
        if re.search(r"\b(address is|it'?s|it is)\b", text, re.I) else text
    t = strip_filler(t)
    t = words_to_digits(t)
    t = re.sub(r"\s+,", ",", t)
    if not re.search(r"\d", t) or len(re.findall(r"[a-z]{2,}", t.lower())) < 2:
        return Parsed(False, error="I need the street number, street name and suburb.")
    words = []
    for w in t.split():
        words.append(w.upper() if w.lower() in ("nsw", "vic", "qld", "sa", "wa", "tas", "act", "nt") else
                     (w[0].upper() + w[1:] if w[0].isalpha() else w))
    addr = " ".join(words).strip(" .")
    pc = ctx.get("postcode")
    if pc and pc not in addr:
        addr = f"{addr} {pc}"
    return Parsed(True, addr, addr, addr)


# ---------------------------------------------------------------- money / retailer / plan
def parse_money(text: str, spec: dict, ctx: dict) -> Parsed:
    if SKIP_WORDS.search(text):
        return Parsed(False, skip=True)
    s = words_to_digits(text)
    m = re.search(r"\$?\s*(\d{1,5})(?:\.(\d{1,2}))?", s)
    if not m:
        return Parsed(False, error="An estimate in dollars is fine, or say skip.")
    v = int(m.group(1))
    return Parsed(True, v, f"${v}", f"{v} dollars")


RETAILERS = ["agl", "origin", "energyaustralia", "energy australia", "red energy", "alinta", "simply energy",
             "lumo", "powershop", "momentum", "tango", "dodo", "sumo", "globird", "ovo", "amber", "diamond",
             "click energy", "kogan", "1st energy", "nectr", "covau", "powerclub", "ergon", "aurora", "actewagl",
             "retailer 1", "retailer 2", "retailer 3"]


def parse_retailer(text: str, spec: dict, ctx: dict) -> Parsed:
    t = words_to_digits(text.lower())
    for r in RETAILERS:
        if re.search(r"\b" + re.escape(r) + r"\b", t):
            name = r.upper() if r == "agl" else r.title()
            return Parsed(True, name, name, name)
    if SKIP_WORDS.search(text):
        return Parsed(True, "Unknown", "Unknown", "you're not sure")
    cleaned = re.sub(r"^(it'?s|it is|we'?re with|i'?m with|with|currently|at the moment)\s+", "", strip_filler(text), flags=re.I)
    cleaned = re.sub(r"\b(at the moment|currently|i think)\b", "", cleaned, flags=re.I).strip(" .,")
    if 0 < len(cleaned.split()) <= 4 and not re.search(r"\?", text):
        name = cleaned.title()
        return Parsed(True, name, name, name)
    return Parsed(False, error="")


def parse_plan(text: str, spec: dict, ctx: dict) -> Parsed:
    options = ctx.get("plan_options") or []
    t = words_to_digits(text.lower(), ordinals=True)
    for i, p in enumerate(options):
        if re.search(r"\b" + re.escape(p["retailer"].lower()) + r"\b", t) or \
           re.search(r"\b" + re.escape(p["name"].lower()) + r"\b", t):
            return Parsed(True, p["id"], f"{p['retailer']} {p['name']}", f"{p['retailer']} {p['name']}", extra={"index": i})
    idx = None
    if re.search(r"\b(last|bottom)\b", t):
        idx = len(options) - 1
    elif re.search(r"\b(cheapest|top|lowest)\b", t):
        idx = 0
    else:
        m = re.search(r"\b([1-9])\b", t)
        if m:
            idx = int(m.group(1)) - 1
    if idx is not None and 0 <= idx < len(options):
        p = options[idx]
        return Parsed(True, p["id"], f"{p['retailer']} {p['name']}", f"{p['retailer']} {p['name']}", extra={"index": idx})
    return Parsed(False, error="")


PARSERS = {
    "yes_no": parse_yes_no, "enum": parse_enum, "postcode": parse_postcode, "mobile": parse_mobile,
    "email": parse_email, "full_name": parse_full_name, "dob": parse_dob, "future_date": parse_future_date,
    "address": parse_address, "money": parse_money, "retailer": parse_retailer, "plan": parse_plan,
}


def parse(ftype: str, text: str, spec: Optional[dict] = None, ctx: Optional[dict] = None) -> Parsed:
    return PARSERS[ftype](text, spec or {}, ctx or {})
