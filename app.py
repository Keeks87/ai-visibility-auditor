import json
import re
from urllib.parse import urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AI Visibility Auditor MVP)"
}


def fetch_page(url: str, timeout: int = 15):
    response = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    return response


def get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def extract_visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def extract_meta_content(soup: BeautifulSoup, name: str = None, prop: str = None) -> str:
    tag = None
    if name:
        tag = soup.find("meta", attrs={"name": re.compile(f"^{re.escape(name)}$", re.I)})
    if not tag and prop:
        tag = soup.find("meta", attrs={"property": re.compile(f"^{re.escape(prop)}$", re.I)})
    return tag.get("content", "").strip() if tag else ""


def extract_canonical(soup: BeautifulSoup) -> str:
    tag = soup.find("link", rel=lambda x: x and "canonical" in [r.lower() for r in (x if isinstance(x, list) else [x])])
    return tag.get("href", "").strip() if tag else ""


def detect_structured_data(html: str, soup: BeautifulSoup):
    schema_types = []

    # JSON-LD
    for script in soup.find_all("script", attrs={"type": re.compile("application/ld\\+json", re.I)}):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if isinstance(item, dict):
                    t = item.get("@type")
                    if isinstance(t, list):
                        schema_types.extend([str(x) for x in t])
                    elif t:
                        schema_types.append(str(t))
            if items:
                continue
        except Exception:
            pass

    # Very light microdata detection
    if soup.find(attrs={"itemscope": True}) or soup.find(attrs={"itemtype": True}):
        schema_types.append("Microdata")

    cleaned = []
    seen = set()
    for item in schema_types:
        if item not in seen:
            seen.add(item)
            cleaned.append(item)

    return {
        "present": len(cleaned) > 0,
        "types": cleaned
    }


def count_links(soup: BeautifulSoup, base_url: str):
    base_domain = get_domain(base_url)
    internal = 0
    external = 0

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()

        if not href or low.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        if href.startswith("/"):
            internal += 1
            continue

        parsed_domain = get_domain(href)
        if parsed_domain and parsed_domain == base_domain:
            internal += 1
        elif parsed_domain:
            external += 1

    return internal, external


def extract_entities_simple(text: str):
    # Lightweight heuristic: repeated capitalised terms / product-like entities
    candidates = re.findall(r"\b[A-Z][a-zA-Z0-9&'-]{2,}\b", text)
    freq = {}
    for c in candidates:
        freq[c] = freq.get(c, 0) + 1
    common = sorted(freq.items(), key=lambda x: (-x[1], x[0]))[:10]
    return [c for c, _ in common]


def score_structure(h1_count: int, h2_count: int, title_present: bool, meta_present: bool) -> int:
    score = 0
    if title_present:
        score += 3
    if meta_present:
        score += 2
    if h1_count == 1:
        score += 3
    elif h1_count > 1:
        score += 1
    if h2_count >= 2:
        score += 2
    return min(score, 10)


def score_crawlability(status_code: int, noindex: bool, canonical_present: bool) -> int:
    score = 0
    if 200 <= status_code < 300:
        score += 5
    elif 300 <= status_code < 400:
        score += 3
    if canonical_present:
        score += 3
    if not noindex:
        score += 2
    return min(score, 10)


def score_internal_linking(internal_links: int) -> int:
    if internal_links >= 15:
        return 10
    if internal_links >= 10:
        return 8
    if internal_links >= 5:
        return 6
    if internal_links >= 2:
        return 4
    if internal_links >= 1:
        return 2
    return 0


def score_clarity(text: str) -> int:
    word_count = len(text.split())
    avg_sentence_length = 0
    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if sentences:
        avg_sentence_length = sum(len(s.split()) for s in sentences) / len(sentences)

    score = 5
    if word_count >= 300:
        score += 1
    if word_count >= 700:
        score += 1
    if 8 <= avg_sentence_length <= 22:
        score += 2
    if re.search(r"\b(what|how|why|when|where|can|should|best)\b", text, re.I):
        score += 1
    return min(score, 10)


def score_answer_focus(text: str) -> int:
    score = 0
    patterns = [
        r"\bwhat is\b",
        r"\bhow to\b",
        r"\bwhy\b",
        r"\bbenefits?\b",
        r"\bfaq\b",
        r"\bquestions?\b",
        r"\banswers?\b",
    ]
    matches = sum(1 for p in patterns if re.search(p, text, re.I))
    if matches >= 5:
        score = 10
    elif matches == 4:
        score = 8
    elif matches == 3:
        score = 6
    elif matches == 2:
        score = 4
    elif matches == 1:
        score = 2
    return score


def score_entity_relevance(entities) -> int:
    count = len(entities)
    if count >= 8:
        return 10
    if count >= 6:
        return 8
    if count >= 4:
        return 6
    if count >= 2:
        return 4
    if count >= 1:
        return 2
    return 0


def overall_score(structure, crawlability, linking, clarity, answer_focus, entity_relevance):
    technical = (structure + crawlability + linking) / 3
    content = (clarity + answer_focus + entity_relevance) / 3
    return round((technical * 0.45 + content * 0.55) * 10, 1)


def generate_recommendations(data: dict):
    recs = []

    if not data["title"]:
        recs.append("Add a clear title tag that states the main topic of the page.")
    if not data["meta_description"]:
        recs.append("Add a meta description that summarises the page in a direct, click-worthy way.")
    if data["h1_count"] == 0:
        recs.append("Add a single H1 that clearly reflects the main intent of the page.")
    elif data["h1_count"] > 1:
        recs.append("Reduce multiple H1s to a single primary H1 and use H2s for subtopics.")
    if data["h2_count"] < 2:
        recs.append("Improve heading hierarchy with more H2 subheadings to make the content easier to scan.")
    if not data["structured_data_present"]:
        recs.append("Add structured data where relevant, such as Product, FAQ, Article, or Breadcrumb schema.")
    if data["internal_links"] < 3:
        recs.append("Add more relevant internal links to strengthen context and crawl paths.")
    if data["noindex"]:
        recs.append("Remove the noindex directive if this page is meant to be discoverable.")
    if data["answer_focus_score"] <= 4:
        recs.append("Add direct question-and-answer style content near the top of the page.")
    if data["clarity_score"] <= 5:
        recs.append("Rewrite sections to be more direct, concise, and easier for both users and AI systems to summarise.")
    if data["entity_relevance_score"] <= 5:
        recs.append("Include more specific entities, product details, and contextual terms related to the page topic.")

    return recs[:5]


def audit_url(url: str):
    response = fetch_page(url)
    html = response.text
    soup = BeautifulSoup(html, "lxml")

    title = soup.title.get_text(strip=True) if soup.title else ""
    meta_description = extract_meta_content(soup, name="description")
    robots = extract_meta_content(soup, name="robots")
    canonical = extract_canonical(soup)

    h1_tags = soup.find_all("h1")
    h2_tags = soup.find_all("h2")
    h1_text = h1_tags[0].get_text(" ", strip=True) if h1_tags else ""

    visible_text = extract_visible_text(soup)
    word_count = len(visible_text.split())
    internal_links, external_links = count_links(soup, str(response.url))

    structured = detect_structured_data(html, soup)
    entities = extract_entities_simple(visible_text[:5000])

    noindex = "noindex" in robots.lower()
    nofollow = "nofollow" in robots.lower()

    structure_score = score_structure(len(h1_tags), len(h2_tags), bool(title), bool(meta_description))
    crawlability_score = score_crawlability(response.status_code, noindex, bool(canonical))
    internal_linking_score = score_internal_linking(internal_links)

    clarity_score = score_clarity(visible_text[:5000])
    answer_focus_score = score_answer_focus(visible_text[:5000])
    entity_relevance_score = score_entity_relevance(entities)

    final_score = overall_score(
        structure_score,
        crawlability_score,
        internal_linking_score,
        clarity_score,
        answer_focus_score,
        entity_relevance_score,
    )

    result = {
        "final_url": str(response.url),
        "status_code": response.status_code,
        "title": title,
        "meta_description": meta_description,
        "canonical": canonical,
        "robots": robots,
        "noindex": noindex,
        "nofollow": nofollow,
        "h1": h1_text,
        "h1_count": len(h1_tags),
        "h2_count": len(h2_tags),
        "word_count": word_count,
        "internal_links": internal_links,
        "external_links": external_links,
        "structured_data_present": structured["present"],
        "structured_data_types": structured["types"],
        "entities": entities,
        "structure_score": structure_score,
        "crawlability_score": crawlability_score,
        "internal_linking_score": internal_linking_score,
        "clarity_score": clarity_score,
        "answer_focus_score": answer_focus_score,
        "entity_relevance_score": entity_relevance_score,
        "overall_ai_visibility_score": final_score,
    }

    result["recommendations"] = generate_recommendations(result)
    return result


st.set_page_config(page_title="AI Visibility Auditor MVP", page_icon="🔎", layout="wide")

st.title("🔎 AI Visibility Auditor MVP")
st.write("Paste a URL to run a lightweight technical + content audit for AI visibility.")

url = st.text_input("Enter a URL", placeholder="https://example.com/page")

if st.button("Run Audit", type="primary"):
    if not url.strip():
        st.error("Please enter a URL.")
    elif not re.match(r"^https?://", url.strip(), re.I):
        st.error("Please enter a full URL starting with http:// or https://")
    else:
        try:
            with st.spinner("Auditing page..."):
                data = audit_url(url.strip())

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Overall AI Visibility", f'{data["overall_ai_visibility_score"]}/100')
            c2.metric("Clarity", f'{data["clarity_score"]}/10')
            c3.metric("Structure", f'{data["structure_score"]}/10')
            c4.metric("Entity Relevance", f'{data["entity_relevance_score"]}/10')

            with st.expander("Technical AI SEO Checks", expanded=True):
                st.write(f'**Final URL:** {data["final_url"]}')
                st.write(f'**Status code:** {data["status_code"]}')
                st.write(f'**Title present:** {"Yes" if data["title"] else "No"}')
                st.write(f'**Meta description present:** {"Yes" if data["meta_description"] else "No"}')
                st.write(f'**Canonical present:** {"Yes" if data["canonical"] else "No"}')
                st.write(f'**Noindex:** {"Yes" if data["noindex"] else "No"}')
                st.write(f'**H1 count:** {data["h1_count"]}')
                st.write(f'**H2 count:** {data["h2_count"]}')
                st.write(f'**Internal links:** {data["internal_links"]}')
                st.write(f'**External links:** {data["external_links"]}')
                st.write(f'**Structured data present:** {"Yes" if data["structured_data_present"] else "No"}')
                if data["structured_data_types"]:
                    st.write("**Structured data types:** " + ", ".join(data["structured_data_types"]))

            with st.expander("Content Analysis", expanded=True):
                st.write(f'**Word count:** {data["word_count"]}')
                st.write(f'**Answer focus score:** {data["answer_focus_score"]}/10')
                st.write(f'**Clarity score:** {data["clarity_score"]}/10')
                st.write(f'**Entity relevance score:** {data["entity_relevance_score"]}/10')
                if data["entities"]:
                    st.write("**Detected entities:** " + ", ".join(data["entities"]))

            with st.expander("Recommendations", expanded=True):
                if data["recommendations"]:
                    for rec in data["recommendations"]:
                        st.write(f"- {rec}")
                else:
                    st.write("No major issues found in this MVP audit.")

            with st.expander("Raw Extracted Data"):
                st.json(data)

        except requests.RequestException as e:
            st.error(f"Request failed: {e}")
        except Exception as e:
            st.error(f"Something went wrong: {e}")
