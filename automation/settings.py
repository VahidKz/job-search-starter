"""Apply per-user search settings without writing to the shipped source catalog."""

import json
import re
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
BOARD_SLUG = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]|\.(?=[A-Za-z0-9])){0,99}")
PLATFORMS = {"ashby", "greenhouse", "greenhouse_eu", "lever", "factorial", "workable", "bamboohr"}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


@lru_cache(maxsize=1)
def profile(required=False):
    path = LOCAL / "profile.json"
    if path.exists():
        data = read_json(path)
        if isinstance(data, dict) and data.get("confirmed") is True:
            titles = terms(data.get("target_titles"), "target_titles")
            if titles:
                return {**data, "target_titles": titles,
                        "selected_lanes": terms(data.get("selected_lanes", ["global"]), "selected_lanes")}
    if required:
        sys.exit("Confirm at least one target title in .local/profile.json before scanning.")
    return {}


def terms(value, field):
    if not isinstance(value, list) or not all(isinstance(t, str) and t.strip() for t in value):
        raise ValueError(f"{field} must be an array of non-empty strings")
    return list(dict.fromkeys(t.strip() for t in value))


def term_matches(term, text):
    pattern = re.escape(term).replace(r"\ ", r"[\s-]+")
    return re.search(r"(?<!\w)" + pattern + r"(?!\w)", text, re.I) is not None


def chunks(prefix, values):
    return [{"name": f"{prefix}-{n // 4 + 1}", "terms": values[n:n + 4]}
            for n in range(0, len(values), 4)]


def config_data(name, require_profile=False):
    base = ROOT / "config" / name
    override = LOCAL / "config" / name
    data = read_json(override if override.exists() else base)
    user = profile(required=require_profile)
    titles = user.get("target_titles", [])
    aliases = terms(user.get("title_aliases", []), "title_aliases")
    skills = terms(user.get("search_terms", []), "search_terms")
    excluded = terms(user.get("excluded_titles", []), "excluded_titles")
    title_terms = list(dict.fromkeys(titles + aliases))
    locations = terms(user.get("search_locations", []), "search_locations")
    markets = user.get("markets", {})
    if not isinstance(markets, dict) or any(not SLUG.fullmatch(key) or not isinstance(value, dict)
                                            for key, value in markets.items()):
        raise ValueError("markets must map valid lane slugs to objects")

    if name == "search-config.json":
        data.update(target_titles=titles, title_terms=title_terms, excluded_titles=excluded)
        fit = user.get("fit_terms", {})
        data["fit_terms"] = {key: terms(fit.get(key, default), f"fit_terms.{key}") for key, default in
                             {"primary": title_terms + skills, "secondary": [],
                              "domain": [], "platform": []}.items()}
        for key, market in markets.items():
            lane = data["lanes"].setdefault(key, {"description": key, "countries": []})
            for attr in ("locations", "countries", "remote_only", "queries"):
                if attr in market:
                    lane[attr] = market[attr]
        for key, lane in data["lanes"].items():
            market = markets.get(key, {})
            if key == "global" and "locations" not in market:
                lane["locations"] = locations
            lane["queries"] = terms(market.get("queries", []) or lane.get("queries", []) or title_terms,
                                    f"{key}.queries")
        if user.get("default_days"):
            data["default_days"] = user["default_days"]
    elif name == "search-engine-ats.json":
        data["role_packs"] = chunks("role", title_terms) + chunks("skill", skills)
        data["title_packs"] = chunks("title", title_terms)
        if locations:
            data["lane_filters"]["global"]["locations"] = locations
        # Sponsorship is a mandatory gate only for users who need it.
        if user.get("require_sponsorship") is not True:
            for lane in data["lane_filters"].values():
                lane["mobility"] = [term for term in lane.get("mobility", [])
                                    if term in {"remote", "contractor", "worldwide", "global remote",
                                                "international contractor"}]
        for key, market in markets.items():
            lane = data["lane_filters"].setdefault(key, {"locations": [], "mobility": []})
            for attr in ("locations", "mobility"):
                if attr in market:
                    lane[attr] = terms(market[attr], f"{key}.{attr}")
    elif name == "agent-lanes.json":
        for key, market in markets.items():
            row = data["lanes"].setdefault(key, {"title": key, "objective": "Search this market.",
                                                  "cadence_days": 1, "public_feeds": [],
                                                  "sponsor_registers": [], "manual_sources": []})
            for attr in ("title", "objective", "cadence_days", "public_feeds", "sponsor_registers", "manual_sources"):
                if attr in market:
                    row[attr] = market[attr]
    elif name == "ats-boards.json":
        data["boards"] += user.get("boards", [])
        boards = {}
        for board in data["boards"]:
            if board.get("kind") not in PLATFORMS or not BOARD_SLUG.fullmatch(board.get("slug", "")):
                raise ValueError("Employer boards require a supported kind and a board slug, not a URL")
            identity = (board["kind"], board.get("instance"), board["slug"].casefold())
            boards[identity] = board
        data["boards"] = list(boards.values())
    elif name == "job-radar.json":
        search = config_data("search-config.json")
        data.update(titles=title_terms, excluded_titles=excluded, title_terms=title_terms,
                    target_titles=titles, fit_terms=search["fit_terms"],
                    official_ats_hosts=search["official_ats_hosts"], aggregator_hosts=search["aggregator_hosts"])
    return data
