import os
import time
import logging
import threading
import re
import requests
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("eping")
app = Flask(__name__)
CORS(app)

WTO_KEY    = os.getenv("WTO_API_KEY", "")
CLAUDE_KEY = os.getenv("CLAUDE_API_KEY", "")
CACHE_TTL  = 3600
_cache     = {"data": [], "at": 0}
_lock      = threading.Lock()


def cache_fresh():
    return (time.time() - _cache["at"]) < CACHE_TTL and bool(_cache["data"])


def build_docs(sym, doc_link="", dol_link="", link_to_notif=""):
    docs = []
    if doc_link:
        # WTO يفصل الروابط بفاصلة أو سطر جديد أو كليهما
        raw = doc_link.replace("\r\n", "\n").replace("\r", "\n")
        # فصل بسطر جديد أولاً ثم بفاصلة
        parts = []
        for line in raw.split("\n"):
            for part in line.split(","):
                part = part.strip()
                if part.startswith("http"):
                    parts.append(part)
        for i, url in enumerate(parts):
            label = "مستند رسمي" if len(parts) == 1 else "مستند رسمي (" + str(i+1) + ")"
            docs.append({"name": label, "url": url, "type": "pdf"})
    # إذا لا يوجد رابط http، نستخدم dolLink
    if not docs and dol_link:
        dol_clean = dol_link.replace("\\", "/")
        dol_url = "https://docs.wto.org/dol2fe/Pages/SS/directdoc.aspx?filename=q:/" + dol_clean
        docs.append({"name": "وثيقة WTO", "url": dol_url, "type": "doc"})
    return docs


def parse_item(it):
    sym      = (it.get("documentSymbol") or it.get("symbol") or "").strip()
    area     = it.get("area", "")
    ntype    = "SPS" if (area == "SPS" or "/SPS/" in sym) else "TBT"

    # العنوان — نستخدم titlePlain أولاً (بدون HTML)
    title_en = (it.get("titlePlain") or it.get("title") or it.get("titleEnglish") or sym or "").strip()
    # إزالة HTML بسيطة إذا وُجد
    if "<" in title_en:
        title_en = re.sub(r"<[^>]+>", " ", title_en).strip()

    # المنتجات
    prods = it.get("productsFreeTextPlain") or it.get("productsFreeText") or ""
    if isinstance(prods, str):
        prods = [p.strip() for p in re.split(r"[,;،]", prods) if p.strip()][:5]
    elif not isinstance(prods, list):
        prods = []

    # الكلمات المفتاحية
    kws_raw = it.get("keywords") or it.get("spsKeywords") or []
    kws = [k.get("name","") for k in kws_raw if isinstance(k, dict) and k.get("name")][:6]

    # نوع الإخطار
    notif_type = it.get("notificationType") or ""

    # التواريخ
    date_raw = it.get("distributionDate") or ""
    dead_raw = it.get("commentDeadlineDate") or ""

    # الحالة — commentDeadlineDate موجود = مفتوح للتعليق
    is_open = bool(dead_raw) or bool(it.get("isOpenForComments"))

    # المستندات
    doc_link      = it.get("notifiedDocumentLink") or ""
    dol_link      = it.get("dolLink") or ""
    link_to_notif = it.get("linkToNotification") or ""

    # رابط ePing
    sym_clean = sym.strip()
    eping_link = link_to_notif or (
        "https://eping.wto.org/en/Search/Index?documentSymbol=" +
        requests.utils.quote(sym_clean) if sym_clean else ""
    )

    return {
        "id":              sym or str(it.get("id", "")),
        "symbol":          sym,
        "member":          it.get("notifyingMember") or it.get("member") or "",
        "memberCode":      it.get("notifyingMemberCode") or it.get("memberCode") or "",
        "date":            date_raw[:10] if date_raw and len(date_raw) >= 10 else date_raw,
        "type":            ntype,
        "notifType":       notif_type,
        "title":           title_en,
        "titleEn":         title_en,
        "titleAr":         "",
        "status":          "مفتوح للتعليق" if is_open else "منتهي",
        "products":        prods,
        "keywords":        kws,
        "commentDeadline": dead_raw[:10] if dead_raw and len(dead_raw) >= 10 else "",
        "docs":            build_docs(sym, doc_link, dol_link, link_to_notif),
        "epingLink":       eping_link,
    }


def extract_rows(d):
    if isinstance(d, list):
        return d
    if not isinstance(d, dict):
        return []
    for key in ["items", "notifications", "rows", "data", "results", "content"]:
        val = d.get(key)
        if val is not None:
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                for k2 in ["items", "notifications", "rows", "data"]:
                    v2 = val.get(k2)
                    if isinstance(v2, list):
                        return v2
    return []


def fetch_data():
    headers  = {"Ocp-Apim-Subscription-Key": WTO_KEY, "Accept": "application/json"}
    all_data = []
    for pg in range(1, 7):
        try:
            r = requests.get(
                "https://api.wto.org/eping/notifications/search",
                headers=headers,
                params={"page": pg, "pageSize": 50, "language": 1},
                timeout=25
            )
            if r.status_code != 200:
                break
            d    = r.json()
            rows = extract_rows(d)
            if not rows:
                break
            all_data.extend([parse_item(it) for it in rows])
            total = d.get("totalCount", d.get("total", 0)) if isinstance(d, dict) else 0
            if total and len(all_data) >= total:
                break
            time.sleep(0.5)
        except Exception as e:
            log.error("Fetch error: " + str(e))
            break
    return all_data


def refresh(force=False):
    if not force and cache_fresh():
        return
    with _lock:
        if not force and cache_fresh():
            return
        data = fetch_data()
        if data:
            data.sort(key=lambda x: x.get("date", ""), reverse=True)
            _cache["data"] = data
            _cache["at"]   = time.time()
            log.info("Cached " + str(len(data)) + " notifications")


def bg():
    while True:
        try:
            refresh()
        except Exception as e:
            log.error("BG: " + str(e))
        time.sleep(CACHE_TTL)


@app.route("/")
def root():
    return jsonify({"notifications": len(_cache["data"]), "api_key": bool(WTO_KEY), "claude_key": bool(CLAUDE_KEY)})


@app.route("/api/notifications")
def notifs():
    if request.args.get("refresh") == "1":
        refresh(force=True)
    data = list(_cache["data"])
    t  = request.args.get("type", "").upper()
    st = request.args.get("status", "")
    kw = request.args.get("keyword", "").lower()
    mc = request.args.get("member", "").lower()
    pg = max(1, int(request.args.get("page", 1)))
    rw = min(200, int(request.args.get("rows", 100)))
    if t in ("SPS", "TBT"):
        data = [n for n in data if n["type"] == t]
    if st == "open":
        data = [n for n in data if n["status"] == "مفتوح للتعليق"]
    if kw:
        data = [n for n in data if kw in n.get("title", "").lower() or kw in n.get("symbol", "").lower()]
    if mc:
        data = [n for n in data if mc in n.get("member", "").lower()]
    total     = len(data)
    page_data = data[(pg - 1) * rw: pg * rw]
    cached_at = datetime.fromtimestamp(_cache["at"]).isoformat() if _cache["at"] else None
    return jsonify({"notifications": page_data, "total": total, "page": pg, "rows": rw, "pages": (total + rw - 1) // rw, "cached_at": cached_at})


@app.route("/api/stats")
def stats():
    d = _cache["data"]
    return jsonify({"total": len(d), "sps": sum(1 for n in d if n["type"] == "SPS"), "tbt": sum(1 for n in d if n["type"] == "TBT"), "open": sum(1 for n in d if n["status"] == "مفتوح للتعليق")})


@app.route("/api/refresh", methods=["GET", "POST"])
def force_refresh():
    refresh(force=True)
    return jsonify({"ok": True, "total": len(_cache["data"])})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    if not CLAUDE_KEY:
        return jsonify({"error": "No Claude key", "analysis": ""})
    try:
        n     = request.get_json()
        ntype = n.get("type", "")
        # محاولة جلب وقراءة PDF المرفق
        pdf_text = ""
        docs = n.get("docs", [])
        for doc in docs:
            url = doc.get("url", "")
            if "members.wto.org" in url and url.endswith(".pdf"):
                try:
                    pr = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                    if pr.status_code == 200 and len(pr.content) > 1000:
                        # استخراج نص بسيط من PDF
                        import re as re2
                        raw = pr.content.decode("latin-1", errors="ignore")
                        text_parts = re2.findall(r"[A-Za-z؀-ۿ][A-Za-z؀-ۿ\s,.\-:;()/]{20,}", raw)
                        if text_parts:
                            pdf_text = " ".join(text_parts[:50])[:2000]
                            break
                except:
                    pass
        lines = [
            "أنت محلل قانوني متخصص في اتفاقيات منظمة التجارة العالمية.",
            "حلّل إشعار ePing:",
            "الرمز: " + n.get("symbol", "") + " | الدولة: " + n.get("member", "") + " | النوع: " + ("SPS - تدابير صحية" if ntype == "SPS" else "TBT - عوائق تقنية"),
            "التاريخ: " + n.get("date", "") + " | موعد التعليق: " + n.get("commentDeadline", ""),
            "العنوان: " + n.get("title", ""),
            "المنتجات: " + ", ".join(n.get("products", [])),
            "",
            "=== الملخص التنفيذي ===",
            "(4-5 جمل عن جوهر الإشعار وأهميته التجارية)",
            "",
            "=== التحليل القانوني ===",
            "الأساس القانوني في اتفاقية " + ("SPS المادة 5" if ntype == "SPS" else "TBT المادة 2"),
            "التوافق مع معايير " + ("Codex / OIE / IPPC" if ntype == "SPS" else "ISO / IEC"),
            "الأثر على التجارة الدولية",
            "حقوق الدول الأعضاء في الاعتراض",
            "",
            "=== التوصيات ===",
            "3-4 توصيات عملية للدول المتضررة.",
            "اكتب بالعربية الفصحى بأسلوب قانوني احترافي.",
        ]
        if pdf_text:
            lines.append("")
            lines.append("نص من المستند الرسمي المرفق:")
            lines.append(pdf_text[:1000])
        prompt = "\n".join(lines)
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-opus-4-7", "max_tokens": 1000, "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        log.info("Claude analyze status: " + str(r.status_code) + " resp: " + r.text[:300])
        if r.status_code == 200:
            text = r.json()["content"][0]["text"].strip()
            return jsonify({"analysis": text})
        return jsonify({"analysis": "", "error": r.text[:300]})
    except Exception as e:
        return jsonify({"error": str(e), "analysis": ""})
@app.route("/api/wto/search", methods=["GET"])
def wto_search():
    """proxy مباشر لـ WTO ePing API"""
    api_headers = {
        "Ocp-Apim-Subscription-Key": WTO_KEY,
        "Accept": "application/json",
        "User-Agent": "WTO-ePing-Monitor/1.0"
    }
    page_size = min(100, int(request.args.get("pageSize", 50)))
    params = {
        "page":     request.args.get("page", 1),
        "pageSize": page_size,
        "language": 1,
    }
    for p in ["domainIds", "documentSymbol", "distributionDateFrom",
              "distributionDateTo", "countryIds", "hs", "ics", "freeText"]:
        v = request.args.get(p)
        if v:
            params[p] = v
    try:
        r = requests.get(
            "https://api.wto.org/eping/notifications/search",
            headers=api_headers, params=params, timeout=30
        )
        if r.status_code == 200:
            d    = r.json()
            rows = extract_rows(d)
            parsed = [parse_item(it) for it in rows]
            total  = d.get("totalCount", len(parsed))
            cur_pg = int(d.get("currentPage", params["page"]))
            return jsonify({
                "notifications": parsed,
                "total":    total,
                "page":     cur_pg,
                "pageSize": d.get("pageSize", page_size),
                "pages":    max(1, (total + page_size - 1) // page_size),
            })
        return jsonify({"error": r.text[:300], "status": r.status_code, "notifications": []})
    except Exception as e:
        return jsonify({"error": str(e), "notifications": []})


@app.route("/api/translate", methods=["POST"])
def translate():
    if not CLAUDE_KEY:
        return jsonify({"error": "No Claude key", "ar": ""})
    try:
        body = request.get_json()
        text = body.get("text", "")
        if not text:
            return jsonify({"ar": ""})
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-opus-4-7", "max_tokens": 300, "messages": [{"role": "user", "content": "ترجم هذا العنوان إلى العربية الفصحى فقط بدون أي نص إضافي:\n" + text}]},
            timeout=15
        )
        if r.status_code == 200:
            ar = r.json()["content"][0]["text"].strip()
            return jsonify({"ar": ar})
        return jsonify({"ar": ""})
    except Exception as e:
        return jsonify({"error": str(e), "ar": ""})


@app.route("/api/test-claude")
def test_claude():
    import os
    key = CLAUDE_KEY
    if not key:
        return jsonify({"error": "No key", "key_len": 0})
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-opus-4-7", "max_tokens": 10, "messages": [{"role": "user", "content": "test"}]},
            timeout=15
        )
        return jsonify({"status": r.status_code, "key_prefix": key[:12], "key_len": len(key), "resp": r.text[:300]})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/test")
def test():
    headers = {"Ocp-Apim-Subscription-Key": WTO_KEY, "Accept": "application/json"}
    try:
        r = requests.get("https://api.wto.org/eping/notifications/search", headers=headers, params={"page": 1, "pageSize": 2, "language": 1}, timeout=15)
        if r.ok:
            d    = r.json()
            rows = extract_rows(d)
            return jsonify({"status": r.status_code, "ok": True, "rows_count": len(rows), "sample": rows[0] if rows else None})
        return jsonify({"status": r.status_code, "ok": False, "error": r.text[:500]})
    except Exception as e:
        return jsonify({"error": str(e)})


if __name__ == "__main__":
    threading.Thread(target=bg, daemon=True).start()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)



@app.route("/api/analyze-doc", methods=["POST"])
def analyze_doc():
    if not CLAUDE_KEY:
        return jsonify({"error": "No Claude key", "analysis": ""})
    try:
        body    = request.get_json() or {}
        pdf_url = body.get("pdf_url", "").strip()
        pdf_text_from_browser = body.get("pdf_text", "").strip()  # النص من المتصفح
        sym     = body.get("symbol", "")
        member  = body.get("member", "")
        ntype   = body.get("type", "")
        title   = body.get("title", "")
        if not pdf_url and not pdf_text_from_browser:
            return jsonify({"error": "No PDF URL", "analysis": ""})

        # إذا أرسل المتصفح نصاً → نستخدمه مباشرة
        if pdf_text_from_browser and len(pdf_text_from_browser) > 50:
            pdf_text = pdf_text_from_browser
            log.info("Using browser-extracted text: %d chars", len(pdf_text))
        else:
            # نحاول جلب PDF من الخادم
            pdf_text = ""
            urls_to_try = []
            raw_urls = pdf_url.replace("\r\n", "\n").replace("\r", "\n")
            for line in raw_urls.split("\n"):
                for part in line.split(","):
                    part = part.strip()
                    if part.startswith("http"):
                        urls_to_try.append(part)
            if not urls_to_try:
                urls_to_try = [pdf_url] if pdf_url else []

            session = requests.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/pdf,*/*",
                "Referer": "https://www.wto.org/",
            })
            for url in urls_to_try[:3]:
                try:
                    pr = session.get(url, timeout=20, allow_redirects=True)
                    log.info("PDF server-fetch %s -> %d (%d bytes)", url[:60], pr.status_code, len(pr.content))
                    if pr.status_code == 200 and len(pr.content) > 500:
                        raw = pr.content.decode("latin-1", errors="ignore")
                        chunks = re.findall(r"[\x20-\x7E]{12,}", raw)
                        filtered = [c for c in chunks if not c.startswith("/") and len(c.split()) > 1]
                        extracted = " ".join(filtered[:300])[:5000]
                        if len(extracted) > 100:
                            pdf_text = extracted
                            log.info("PDF server-extracted: %d chars", len(pdf_text))
                            break
                except Exception as pe:
                    log.warning("PDF server-fetch failed %s: %s", url[:50], pe)

        if not pdf_text:
            log.warning("All PDF URLs blocked (members.wto.org restricted), analyzing from notification data: %s", sym)
            # نبني نصاً تحليلياً من بيانات الإخطار المتاحة في الطلب
            products_str = body.get("products", "")
            keywords_str = body.get("keywords", "")
            objectives_str = body.get("objectives", "")
            comment_deadline = body.get("commentDeadline", "")
            notif_type = body.get("notifType", "")
            pdf_text = "\n".join(filter(None, [
                "العنوان: " + title,
                "الرمز: " + sym,
                "الدولة المُخطِرة: " + member,
                "نوع الإخطار: " + notif_type if notif_type else "",
                "المنتجات: " + (", ".join(products_str) if isinstance(products_str, list) else str(products_str)) if products_str else "",
                "الكلمات المفتاحية: " + (", ".join(keywords_str) if isinstance(keywords_str, list) else str(keywords_str)) if keywords_str else "",
                "الأهداف: " + (", ".join(objectives_str) if isinstance(objectives_str, list) else str(objectives_str)) if objectives_str else "",
                "موعد التعليق: " + comment_deadline if comment_deadline else "",
                "رابط المستند: " + urls_to_try[0] if urls_to_try else "",
            ]))

        prompt = "\n".join([
            "أنت محلل قانوني متخصص في اتفاقيات منظمة التجارة العالمية (WTO).",
            "حلّل هذا الإخطار الرسمي في إطار اتفاقية " + ("SPS" if ntype=="SPS" else "TBT") + ":",
            "الرمز: " + sym + " | الدولة: " + member,
            "العنوان: " + title,
            "",
            "=== محتوى المستند ===",
            pdf_text,
            "",
            "=== التحليل المطلوب ===",
            "1. ملخص المستند وجوهره",
            "2. المتطلبات والاشتراطات الرئيسية المحددة",
            "3. المنتجات والأسواق المتأثرة",
            "4. التحليل القانوني وفق " + ("المادة 5 SPS ومعايير Codex/OIE/IPPC" if ntype=="SPS" else "المادة 2 TBT ومعايير ISO/IEC"),
            "5. الأثر على صادرات المملكة العربية السعودية",
            "6. التوصيات العملية (3-5 توصيات)",
            "اكتب بالعربية الفصحى بأسلوب قانوني احترافي.",
        ])
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-opus-4-7", "max_tokens": 1800,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=45
        )
        if r.status_code == 200:
            return jsonify({"analysis": r.json()["content"][0]["text"].strip()})
        return jsonify({"analysis": "", "error": r.text[:200]})
    except Exception as e:
        return jsonify({"error": str(e), "analysis": ""})

@app.route("/api/translate-batch", methods=["POST"])
def translate_batch_ep():
    if not CLAUDE_KEY:
        return jsonify({"translations": []})
    try:
        body = request.get_json()
        texts = body.get("texts", [])[:15]
        if not texts:
            return jsonify({"translations": []})
        numbered = "\n".join([str(i+1) + ". " + t for i, t in enumerate(texts)])
        prompt = "ترجم هذه العناوين من الانجليزية للعربية. اكتب الرقم ثم الترجمة فقط:\n" + numbered
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-opus-4-7", "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        if r.status_code == 200:
            resp = r.json()["content"][0]["text"].strip()
            result_lines = [l.strip() for l in resp.split("\n") if l.strip()]
            translations = []
            for line in result_lines:
                clean = re.sub(r"^[0-9]+[.)]\s*", "", line).strip()
                if clean:
                    translations.append(clean)
            if len(translations) == len(texts):
                return jsonify({"translations": translations})
        return jsonify({"translations": []})
    except Exception as e:
        return jsonify({"translations": [], "error": str(e)})



# ── البحث المباشر في التدابير غير الجمركية UNCTAD / WTO ──
def _clean_query_value(v):
    return (v or "").strip()

@app.route("/api/ntm/un/search", methods=["GET"])
def ntm_un_search():
    """إنشاء رابط بحث مباشر لموقع UNCTAD TRAINS داخل المنصة."""
    country = _clean_query_value(request.args.get("country"))
    product = _clean_query_value(request.args.get("product"))
    measure = _clean_query_value(request.args.get("measure"))
    official_url = "https://trainsonline.unctad.org/detailedSearch"
    return jsonify({
        "ok": True,
        "source": "UNCTAD TRAINS",
        "official_url": official_url,
        "query": {
            "country": country,
            "product": product,
            "measure": measure,
        },
        "note": "UNCTAD TRAINS قد لا يدعم تمرير جميع فلاتر البحث عبر URL عام؛ لذلك يتم فتح صفحة البحث الرسمية داخل المنصة مع حفظ مدخلات البحث." 
    })

@app.route("/api/ntm/wto/search", methods=["GET"])
def ntm_wto_search():
    """إنشاء رابط بحث مباشر لموقع WTO i-TIP Goods داخل المنصة."""
    member = _clean_query_value(request.args.get("member"))
    hs = _clean_query_value(request.args.get("hs"))
    measure = _clean_query_value(request.args.get("measure"))
    official_url = "https://i-tip.wto.org/goods/Forms/TableView.aspx?mode=modify&action=search"
    return jsonify({
        "ok": True,
        "source": "WTO i-TIP Goods",
        "official_url": official_url,
        "query": {
            "member": member,
            "hs": hs,
            "measure": measure,
        },
        "note": "i-TIP يعتمد على ASP.NET session وقد لا يقبل كل الفلاتر كمعاملات URL مستقرة؛ لذلك يتم فتح البحث الرسمي داخل المنصة مع رابط مباشر." 
    })

# ── التنبيهات ──
_alerts = []
_alert_id = 0

@app.route("/api/alerts", methods=["GET"])
def get_alerts():
    return jsonify({"alerts": _alerts})

@app.route("/api/alerts", methods=["POST"])
def add_alert():
    global _alert_id
    body = request.get_json() or {}
    _alert_id += 1
    alert = {
        "id": _alert_id,
        "type": body.get("type", "الكل"),
        "sector": body.get("sector", ""),
        "country": body.get("country", "جميع الدول"),
        "frequency": body.get("frequency", "فوري"),
        "active": True,
        "created": datetime.now().strftime("%Y-%m-%d")
    }
    _alerts.append(alert)
    return jsonify({"ok": True, "alert": alert})

@app.route("/api/alerts/<int:aid>", methods=["PUT"])
def toggle_alert(aid):
    for a in _alerts:
        if a["id"] == aid:
            a["active"] = not a["active"]
            return jsonify({"ok": True, "alert": a})
    return jsonify({"error": "not found"}), 404

@app.route("/api/alerts/<int:aid>", methods=["DELETE"])
def delete_alert(aid):
    global _alerts
    _alerts = [a for a in _alerts if a["id"] != aid]
    return jsonify({"ok": True})


# ── بدء التشغيل مع gunicorn ──
def startup():
    def _init():
        import time as t
        t.sleep(2)
        try:
            refresh(force=True)
            log.info("STARTUP: %d notifications loaded", len(_cache["data"]))
        except Exception as e:
            log.error("STARTUP error: %s", e)
        RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "")
        ping_interval = 840
        last_ping = time.time()
        last_refresh = time.time()
        while True:
            try:
                time.sleep(60)
                now = time.time()
                if RENDER_URL and (now - last_ping) >= ping_interval:
                    try:
                        requests.get(RENDER_URL + "/api/refresh", timeout=10)
                        last_ping = now
                    except Exception:
                        pass
                if (now - last_refresh) >= CACHE_TTL:
                    refresh()
                    last_refresh = now
            except Exception as e:
                log.error("BG error: %s", e)
    threading.Thread(target=_init, daemon=True, name="wto-fetcher").start()

startup()
