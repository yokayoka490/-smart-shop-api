from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import anthropic
import asyncio
import os
import json
import re
from dotenv import load_dotenv

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
        max_tokens=600,
        system="あなたはショッピングアドバイザーです。ユーザーのニーズから検索パラメータを抽出します。必ずJSON形式のみで返してください。",
        messages=[{
            "role": "user",
            "content": f"""以下のニーズを分析して検索パラメータをJSONで返してください。

ニーズ：{needs}

返却形式（JSONのみ）：
{{
  "keyword": "検索キーワード（日本語・20文字以内）",
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
        return {"recommendations": [], "overall_comment": "条件に合う商品が見つかりませんでした。"}

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
        max_tokens=800,
        system="あなたは親身なショッピングアドバイザーです。ユーザーのニーズに本当に合う商品を選びます。JSONのみ返してください。",
        messages=[{
            "role": "user",
            "content": f"""ユーザーのニーズに最も合う商品を最大3つ選んでください。

ニーズ：{needs}
重要な条件：{req_text}

候補：
{products_text}

返却形式（JSONのみ）：
{{
  "recommendations": [
    {{
      "rank": 1,
      "product_index": 番号（1始まり）,
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

    # Step 3: AIが推薦を生成
    rec_result = await recommend_products(
        needs=needs,
        key_requirements=interpreted.get("key_requirements", []),
        products=products,
    )

    # Step 4: 推薦商品を先頭に並べる
    rec_indices = [r["product_index"] - 1 for r in rec_result.get("recommendations", [])]
    recommended = [products[i] for i in rec_indices if i < len(products)]
    others = [p for i, p in enumerate(products) if i not in rec_indices]

    # 推薦理由をくっつける
    for rec in rec_result.get("recommendations", []):
        idx = rec["product_index"] - 1
        if idx < len(products):
            products[idx]["aiReason"] = rec.get("reason", "")
            products[idx]["aiHighlight"] = rec.get("highlight", "")
            products[idx]["aiCaution"] = rec.get("caution")
            products[idx]["isRecommended"] = True

    return {
        "intent": interpreted,
        "products": recommended + others[:7],
        "overall_comment": rec_result.get("overall_comment", ""),
        "recommended_count": len(recommended),
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
