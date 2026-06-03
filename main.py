from fastapi import FastAPI, Query, Body
from fastapi.middleware.cors import CORSMiddleware
import httpx
import anthropic
import asyncio
import os
import json
import re
from dotenv import load_dotenv
from collections import defaultdict

load_dotenv()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

RAKUTEN_APP_ID = os.getenv("RAKUTEN_APP_ID")
YAHOO_APP_ID = os.getenv("YAHOO_APP_ID")
RAKUTEN_ACCESS_KEY = os.getenv("RAKUTEN_ACCESS_KEY")
RAKUTEN_AFFILIATE_ID = os.getenv("RAKUTEN_AFFILIATE_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SITE_URL = "https://repository-name-smart-shop-g2el.vercel.app"
RAKUTEN_API_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260401"
RAKUTEN_HEADERS = {"Origin": SITE_URL, "Referer": SITE_URL}

ai_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


def calc_effective_price(base_price: int, shipping: int, point_rate: int) -> dict:
    points = int(base_price * point_rate / 100)
    return {
        "basePrice": base_price,
        "shippingCost": shipping,
        "pointReduction": points,
        "effectivePrice": base_price + shipping - points,
    }


async def interpret_needs(needs: str) -> dict:
    """ユーザーのニーズを自然言語で受け取り、検索パラメータを抽出する"""
    res = await ai_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=700,
        system="""あなたはECショッピングアドバイザーです。楽天・Yahoo!ショッピングで購入できる「物（商品）」の検索パラメータを抽出します。
重要：このサービスは物販ECサイトの商品検索のみ対応です。飲食店・サービス・体験・デジタルコンテンツは対象外です。
必ずJSON形式のみで返してください。""",
        messages=[{
            "role": "user",
            "content": f"""以下のニーズを分析してJSONで返してください。

ニーズ：{needs}

ルール：
- 飲食店・レストラン・サービス業など「物ではないもの」への要望は is_out_of_scope: true にする
- keywordは商品カテゴリ名を中心に。価格だけのクエリは「プレゼント ギフト」に置き換える
- keywordに用途・特性を含める（例：「イヤホン」→「ワイヤレスイヤホン ノイズキャンセリング」）

返却形式（JSONのみ）：
{{
  "is_out_of_scope": false,
  "out_of_scope_message": null,
  "keyword": "検索キーワード（日本語・30文字以内）",
  "minPrice": null または整数,
  "maxPrice": null または整数,
  "freeShipping": false,
  "genreId": "",
  "key_requirements": ["条件1", "条件2"],
  "intent_summary": "求めているものの本質（30文字以内）"
}}"""
        }]
    )
    text = res.content[0].text
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        return {"keyword": needs[:20], "minPrice": None, "maxPrice": None,
                "freeShipping": False, "genreId": "", "key_requirements": [], "intent_summary": needs[:30]}
    return json.loads(match.group())


async def search_yahoo_raw(keyword: str, min_price, max_price, free_shipping: bool) -> list:
    params = {
        "appid": YAHOO_APP_ID,
        "query": keyword,
        "hits": 20,
        "sort": "-score",
    }
    if min_price: params["price_from"] = min_price
    if max_price: params["price_to"] = max_price
    if free_shipping: params["shipping"] = "free"

    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch",
                params=params,
                timeout=10,
            )
            data = res.json()
    except Exception:
        return []

    results = []
    for item in data.get("hits", []):
        base = item.get("price", 0)
        shipping_cost = 0 if item.get("shipping", {}).get("code") == "1" else 650
        point_amount = item.get("point", {}).get("amount", 0)
        effective = base + shipping_cost - point_amount
        results.append({
            "id": f"yahoo_{item.get('code', '')}",
            "name": item.get("name", "")[:60],
            "fullName": item.get("name", ""),
            "platform": "yahoo",
            "imageUrl": item.get("image", {}).get("medium", ""),
            "productUrl": item.get("url", ""),
            "reviewAverage": item.get("review", {}).get("rate", 0),
            "reviewCount": item.get("review", {}).get("count", 0),
            "basePrice": base,
            "shippingCost": shipping_cost,
            "pointReduction": point_amount,
            "effectivePrice": effective,
        })
    return results


async def search_rakuten_raw(keyword: str, min_price, max_price, free_shipping: bool, genre_id: str = "") -> list:
    params = {
        "applicationId": RAKUTEN_APP_ID,
        "accessKey": RAKUTEN_ACCESS_KEY,
        "affiliateId": RAKUTEN_AFFILIATE_ID,
        "keyword": keyword,
        "hits": 20,
        "sort": "standard",
        "availability": 1,
    }
    if min_price: params["minPrice"] = min_price
    if max_price: params["maxPrice"] = max_price
    if free_shipping: params["postageFlag"] = 1
    if genre_id: params["genreId"] = genre_id

    async with httpx.AsyncClient() as client:
        res = await client.get(RAKUTEN_API_URL, params=params, headers=RAKUTEN_HEADERS, timeout=10)
        data = res.json()

    results = []
    for item in data.get("Items", []):
        i = item["Item"]
        base = i["itemPrice"]
        shipping = 0 if i.get("postageFlag") == 1 else 650
        calc = calc_effective_price(base, shipping, i.get("pointRate", 1))
        results.append({
            "id": i["itemCode"],
            "name": i["itemName"][:60] + "..." if len(i["itemName"]) > 60 else i["itemName"],
            "fullName": i["itemName"],
            "platform": "rakuten",
            "imageUrl": i["mediumImageUrls"][0]["imageUrl"] if i.get("mediumImageUrls") else "",
            "productUrl": i.get("affiliateUrl") or i["itemUrl"],
            "reviewAverage": i.get("reviewAverage", 0),
            "reviewCount": i.get("reviewCount", 0),
            **calc,
        })
    return results


async def recommend_products(needs: str, key_requirements: list, products: list) -> dict:
    """検索結果からユーザーのニーズに最も合う商品を推薦する"""
    if not products:
        return {"recommendations": [], "overall_comment": "条件に合う商品が見つかりませんでした。", "conditions_unmet": True}

    lines = []
    for i, p in enumerate(products[:15]):
        shipping = "送料無料" if p['shippingCost'] == 0 else f"送料+{p['shippingCost']}円"
        lines.append(
            f"{i+1}. {p['name']} | 実質{p['effectivePrice']}円 | {shipping} | "
            f"★{p.get('reviewAverage', 0):.1f}({p.get('reviewCount', 0)}件)"
        )
    products_text = "\n".join(lines)
    req_text = "・".join(key_requirements) if key_requirements else "なし"

    res = await ai_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        system="あなたは誠実なショッピングアドバイザーです。条件を満たさない商品は正直に報告します。JSONのみ返してください。",
        messages=[{
            "role": "user",
            "content": f"""ユーザーのニーズに最も合う商品を選んでください。

ニーズ：{needs}
重要な条件（必ず確認）：{req_text}

重要なルール：
- 条件が商品名や説明から確認できない場合は正直に報告する
- 全ての重要条件を満たす商品がなければ conditions_unmet: true にする
- conditions_unmetがtrueの場合、どの条件が不足しているか、どの条件を緩めれば見つかるかを説明する

候補：
{products_text}

返却形式（JSONのみ）：
{{
  "conditions_unmet": false,
  "unmet_conditions": [],
  "relax_suggestion": null,
  "priority_options": [],
  "recommendations": [
    {{
      "rank": 1,
      "product_index": 番号（1始まり）,
      "matched_conditions": ["満たしている条件"],
      "unmatched_conditions": ["確認できない条件"],
      "reason": "このニーズに合う理由（60文字以内）",
      "highlight": "特に注目すべき点（25文字以内）",
      "caution": "気になる点があれば（25文字以内、なければnull）"
    }}
  ],
  "overall_comment": "全体的なアドバイス（80文字以内）"
}}"""
        }]
    )
    text = res.content[0].text
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        return {
            "recommendations": [{"rank": 1, "product_index": 1, "reason": "条件に合う商品です", "highlight": "", "caution": None}],
            "overall_comment": ""
        }
    return json.loads(match.group())


@app.get("/api/recommend")
async def recommend(
    needs: str = Query(...),
    minPrice: int | None = Query(default=None),
    maxPrice: int | None = Query(default=None),
    freeShipping: bool = Query(default=False),
    genreId: str | None = Query(default=None),
    sort: str = Query(default="standard"),
):
    """自然言語のニーズ＋絞り込み条件から最適商品を推薦するメインエンドポイント"""
    # Step 1: ニーズを解析
    interpreted = await interpret_needs(needs)

    # 明示的な絞り込み条件はAIの解析より優先する
    final_min = minPrice if minPrice is not None else interpreted.get("minPrice")
    final_max = maxPrice if maxPrice is not None else interpreted.get("maxPrice")
    final_free = freeShipping or interpreted.get("freeShipping", False)
    final_genre = genreId if genreId else interpreted.get("genreId", "")

    # サービス対象外チェック
    if interpreted.get("is_out_of_scope"):
        return {
            "intent": interpreted,
            "products": [],
            "overall_comment": interpreted.get("out_of_scope_message") or "このサービスは楽天・Yahoo!ショッピングの商品検索に特化しています。飲食店・サービス業などは対象外です。購入できる商品について教えてください。",
            "recommended_count": 0,
            "is_out_of_scope": True,
        }

    keyword = interpreted.get("keyword", needs[:20])

    # Step 2: 楽天・Yahoo!を並列検索
    rakuten_results, yahoo_results = await asyncio.gather(
        search_rakuten_raw(keyword=keyword, min_price=final_min, max_price=final_max,
                           free_shipping=final_free, genre_id=final_genre),
        search_yahoo_raw(keyword=keyword, min_price=final_min, max_price=final_max,
                         free_shipping=final_free),
    )
    # 楽天・Yahoo!を交互に並べて両方がAIの視野に入るようにする
    products = [p for pair in zip(rakuten_results, yahoo_results) for p in pair]
    products += rakuten_results[len(yahoo_results):] + yahoo_results[len(rakuten_results):]

    # フォールバック：ゼロ件の場合
    if not products:
        rakuten_fb, yahoo_fb = await asyncio.gather(
            search_rakuten_raw(keyword=keyword, min_price=None, max_price=None,
                               free_shipping=False, genre_id=final_genre),
            search_yahoo_raw(keyword=keyword, min_price=None, max_price=None,
                             free_shipping=False),
        )
        products = [p for pair in zip(rakuten_fb, yahoo_fb) for p in pair]
        products += rakuten_fb[len(yahoo_fb):] + yahoo_fb[len(rakuten_fb):]

    if not products:
        return {
            "intent": interpreted,
            "products": [],
            "recommendations": [],
            "overall_comment": "条件に合う商品が見つかりませんでした。条件を変えてお試しください。"
        }

    has_price_limit = final_min is not None or final_max is not None

    # Step 3: AIが予算内の商品を推薦
    rec_result = await recommend_products(
        needs=needs,
        key_requirements=interpreted.get("key_requirements", []),
        products=products,
    )

    # Step 4: 条件が満たされていない＆予算制限がある場合 → 予算なしで再検索
    extended_products = []
    if rec_result.get("conditions_unmet") and has_price_limit:
        rakuten_ext, yahoo_ext = await asyncio.gather(
            search_rakuten_raw(keyword=keyword, min_price=None, max_price=None,
                               free_shipping=False, genre_id=final_genre),
            search_yahoo_raw(keyword=keyword, min_price=None, max_price=None,
                             free_shipping=False),
        )
        ext_all = [p for pair in zip(rakuten_ext, yahoo_ext) for p in pair]
        ext_all += rakuten_ext[len(yahoo_ext):] + yahoo_ext[len(rakuten_ext):]

        # 予算内の商品と重複しないものだけ
        existing_ids = {p["id"] for p in products}
        ext_candidates = [p for p in ext_all if p["id"] not in existing_ids]

        if ext_candidates:
            ext_rec = await recommend_products(
                needs=needs,
                key_requirements=interpreted.get("key_requirements", []),
                products=ext_candidates,
            )
            ext_indices = [r["product_index"] - 1 for r in ext_rec.get("recommendations", [])]
            for rec in ext_rec.get("recommendations", []):
                idx = rec["product_index"] - 1
                if idx < len(ext_candidates):
                    ext_candidates[idx]["aiReason"] = rec.get("reason", "")
                    ext_candidates[idx]["aiHighlight"] = rec.get("highlight", "")
                    ext_candidates[idx]["aiCaution"] = rec.get("caution")
                    ext_candidates[idx]["isRecommended"] = True
                    ext_candidates[idx]["isExtended"] = True
                    ext_candidates[idx]["matchedConditions"] = rec.get("matched_conditions", [])
                    ext_candidates[idx]["unmatchedConditions"] = rec.get("unmatched_conditions", [])
            extended_products = [ext_candidates[i] for i in ext_indices if i < len(ext_candidates)]

    # Step 5: 予算内の商品に推薦情報をくっつける
    rec_indices = [r["product_index"] - 1 for r in rec_result.get("recommendations", [])]
    recommended = [products[i] for i in rec_indices if i < len(products)]
    others = [p for i, p in enumerate(products) if i not in rec_indices]

    for rec in rec_result.get("recommendations", []):
        idx = rec["product_index"] - 1
        if idx < len(products):
            products[idx]["aiReason"] = rec.get("reason", "")
            products[idx]["aiHighlight"] = rec.get("highlight", "")
            products[idx]["aiCaution"] = rec.get("caution")
            products[idx]["isRecommended"] = True
            products[idx]["matchedConditions"] = rec.get("matched_conditions", [])
            products[idx]["unmatchedConditions"] = rec.get("unmatched_conditions", [])

    # 予算上限を文字列で返す
    budget_label = None
    if final_max:
        budget_label = f"¥{final_max:,}以内"
    elif final_min:
        budget_label = f"¥{final_min:,}以上"

    return {
        "intent": interpreted,
        "products": recommended + others[:5],
        "extended_products": extended_products,
        "overall_comment": rec_result.get("overall_comment", ""),
        "recommended_count": len(recommended),
        "conditions_unmet": rec_result.get("conditions_unmet", False),
        "unmet_conditions": rec_result.get("unmet_conditions", []),
        "relax_suggestion": rec_result.get("relax_suggestion"),
        "priority_options": rec_result.get("priority_options", []),
        "has_price_limit": has_price_limit,
        "budget_label": budget_label,
    }


@app.get("/api/search")
async def search(
    keyword: str = Query(...),
    minPrice: int | None = Query(default=None),
    maxPrice: int | None = Query(default=None),
    freeShipping: bool = Query(default=False),
    minReview: int | None = Query(default=None),
    minReviewAverage: float | None = Query(default=None),
    sort: str = Query(default="standard"),
    genreId: str | None = Query(default=None),
    brand: str | None = Query(default=None),
    color: str | None = Query(default=None),
    madeInJapan: bool = Query(default=False),
):
    full_keyword = keyword
    if brand: full_keyword += f" {brand}"
    if color: full_keyword += f" {color}"
    if madeInJapan: full_keyword += " 日本製"

    params = {
        "applicationId": RAKUTEN_APP_ID,
        "accessKey": RAKUTEN_ACCESS_KEY,
        "keyword": full_keyword,
        "hits": 30,
        "sort": sort,
        "availability": 1,
    }
    if minPrice: params["minPrice"] = minPrice
    if maxPrice: params["maxPrice"] = maxPrice
    if freeShipping: params["postageFlag"] = 1
    if minReview: params["minReviewCount"] = minReview
    if minReviewAverage: params["minReviewAverage"] = minReviewAverage
    if genreId: params["genreId"] = genreId

    async with httpx.AsyncClient() as client:
        res = await client.get(RAKUTEN_API_URL, params=params, headers=RAKUTEN_HEADERS, timeout=10)
        data = res.json()

    results = []
    for item in data.get("Items", []):
        i = item["Item"]
        base = i["itemPrice"]
        shipping = 0 if i.get("postageFlag") == 1 else 650
        calc = calc_effective_price(base, shipping, i.get("pointRate", 1))
        results.append({
            "id": i["itemCode"],
            "name": i["itemName"][:60] + "..." if len(i["itemName"]) > 60 else i["itemName"],
            "platform": "rakuten",
            "imageUrl": i["mediumImageUrls"][0]["imageUrl"] if i.get("mediumImageUrls") else "",
            "productUrl": i.get("affiliateUrl") or i["itemUrl"],
            "reviewAverage": i.get("reviewAverage", 0),
            "reviewCount": i.get("reviewCount", 0),
            **calc,
        })

    if sort == "+itemPrice":
        results = sorted(results, key=lambda x: x["effectivePrice"])
    elif sort == "-itemPrice":
        results = sorted(results, key=lambda x: x["effectivePrice"], reverse=True)
    elif sort == "-reviewCount":
        results = sorted(results, key=lambda x: x.get("reviewCount", 0), reverse=True)

    return {"results": results[:10]}


# フィードバックをメモリに保存（Renderは再起動でリセットされるが無料枠では許容）
feedback_store = defaultdict(lambda: {"helpful": 0, "not_helpful": 0, "comments": []})

@app.post("/api/feedback")
async def post_feedback(
    query: str = Body(...),
    helpful: bool = Body(...),
    comment: str = Body(default=""),
):
    feedback_store[query[:50]]["helpful" if helpful else "not_helpful"] += 1
    if comment.strip():
        feedback_store[query[:50]]["comments"].append(comment[:200])
    return {"status": "ok"}

@app.get("/api/feedback/stats")
async def get_feedback_stats():
    total_helpful = sum(v["helpful"] for v in feedback_store.values())
    total_not = sum(v["not_helpful"] for v in feedback_store.values())
    total = total_helpful + total_not
    rate = round(total_helpful / total * 100) if total > 0 else None
    return {
        "total": total,
        "helpful": total_helpful,
        "not_helpful": total_not,
        "helpful_rate": rate,
        "recent_comments": [
            c for v in list(feedback_store.values())[-10:] for c in v["comments"]
        ][-5:],
    }

@app.get("/api/health")
async def health():
    return {"status": "ok"}

@app.get("/api/debug-both")
async def debug_both(keyword: str = "イヤホン"):
    rakuten, yahoo = await asyncio.gather(
        search_rakuten_raw(keyword=keyword, min_price=None, max_price=None, free_shipping=False),
        search_yahoo_raw(keyword=keyword, min_price=None, max_price=None, free_shipping=False),
    )
    return {
        "rakuten_count": len(rakuten),
        "yahoo_count": len(yahoo),
        "rakuten_first": rakuten[0]["name"] if rakuten else "なし",
        "yahoo_first": yahoo[0]["name"] if yahoo else "なし",
    }
