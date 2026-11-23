"""Build native and indexed discovery tasks, with separate coverage receipts."""

from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from generate_ats_search_queries import or_group
from settings import config_data, profile
from workflow_dates import today_local


def build_manual_plan(lane, days, sources, candidate=None, today=None):
    candidate = candidate if candidate is not None else profile(required=True)
    markets = config_data("search-config.json")["lanes"]
    titles = candidate["target_titles"] + candidate.get("title_aliases", [])
    locations = markets[lane].get("locations", [])
    cutoff = (today or today_local()) - timedelta(days=days)
    tasks = []
    for name, url, _ in sources:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username:
            continue
        jobs = name == "LinkedIn Jobs"
        posts = name == "LinkedIn Hiring Posts"
        sites = ["linkedin.com/posts/", "linkedin.com/feed/update/"] if posts else [
            "linkedin.com/jobs/view/" if jobs else parsed.hostname + parsed.path.rstrip("/")
        ]
        queries = []
        for title in titles:
            for site in sites:
                location_filter = or_group(locations[:3])
                hiring = '("hiring" OR "join our team")' if posts else ""
                queries.append(" ".join(filter(None, (f"site:{site}", or_group([title]),
                                                      location_filter, hiring, f"after:{cutoff}"))))
        native_urls = [url]
        if jobs:
            native_urls = ["https://www.linkedin.com/jobs/search/?" + urlencode({
                "keywords": title, "location": location, "f_TPR": f"r{days * 86400}",
            }) for title in titles for location in locations[:3] or [""]]
        if posts:
            native_urls = [url + "?" + urlencode({"keywords": f"{title} hiring"}) for title in titles]
        tasks.append({"source": name, "native_urls": native_urls, "indexed_queries": queries,
                      "native_status": "pending", "index_status": "pending"})
    return {"lane": lane, "days": days, "tasks": tasks}
