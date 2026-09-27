from types import SimpleNamespace


def test_buyer_looks_up_requested_hoodie_colors_instead_of_reasking(monkeypatch):
    from crew import dialogue

    calls = []
    product = SimpleNamespace(
        title="Unisex Heavy Blend Hoodie | Gildan 18500",
        colors=["Black", "Forest Green", "Navy", "White"],
        sizes=["S", "M", "L"], min_price=22.75, max_price=28.50,
        source_url="https://api.printful.com/products/146",
    )
    monkeypatch.setattr(dialogue, "lookup_printful_catalog", lambda query, **kwargs:
                        calls.append((query, kwargs)) or
                        SimpleNamespace(status="ok", products=[product]))
    monkeypatch.setattr(dialogue, "queue_allowed_flow", lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError("read-only lookup queued a job")))

    reply = dialogue.answer("Buyer", "look up the hoodie colors",
                            user_id="U-owner", channel_id="C-demo",
                            delivery_id="event:hoodie-colors")
    assert "Black, Forest Green, Navy, White" in reply
    assert "https://api.printful.com/products/146" in reply
    assert "Want me to look" not in reply
    assert calls == [("look up the hoodie colors", {"max_products": 2})]

    reply = dialogue.answer("Buyer", "what else can we buy outside of waterbottles? what about hoodies",
                            user_id="U-owner", channel_id="C-demo",
                            delivery_id="event:hoodie-options")
    assert "Unisex Heavy Blend Hoodie" in reply
    assert "Listed variants" in reply


def test_hoodie_purchase_remains_in_purchase_flow(monkeypatch):
    from crew import dialogue

    monkeypatch.setattr(dialogue, "lookup_printful_catalog", lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError("purchase was treated as research")))
    monkeypatch.setattr(dialogue, "queue_allowed_flow", lambda flow, **kwargs: flow)
    assert dialogue.answer("Buyer", "buy 20 hoodies for Berlin",
                           user_id="U-owner", channel_id="C-demo",
                           delivery_id="event:hoodie-buy") == "natural-language"


def test_color_follow_up_uses_prior_product_context(monkeypatch):
    from crew import dialogue

    calls = []
    product = SimpleNamespace(
        title="Unisex Heavy Blend Hoodie | Gildan 18500",
        colors=["Black", "Navy"], sizes=["M"], min_price=22.63,
        max_price=30.63, source_url="https://api.printful.com/products/146",
    )
    monkeypatch.setattr(dialogue, "lookup_printful_catalog", lambda query, **kwargs:
                        calls.append((query, kwargs)) or
                        SimpleNamespace(status="ok", products=[product]))
    monkeypatch.setattr(dialogue, "queue_allowed_flow", lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError("follow-up queued a job")))

    reply = dialogue.answer(
        "Buyer", "what colors?", user_id="U-owner", channel_id="C-demo",
        delivery_id="event:color-follow-up",
        context="USER: what about hoodies\nASSISTANT: I found two hoodie styles.",
    )
    assert "Catalog colors: Black, Navy" in reply
    assert calls == [("what colors? for hoodies", {"max_products": 2})]


def test_unavailable_venue_and_budget_shortfall_take_read_only_next_steps(monkeypatch):
    from crew import dialogue, jobs

    calls = []
    monkeypatch.setattr(jobs, "research_venue_alternatives", lambda **kwargs:
                        calls.append(("venue", kwargs)) or "Three venue leads checked")
    monkeypatch.setattr(jobs, "suggest_budget_reallocation", lambda **kwargs:
                        calls.append(("budget", kwargs)) or "Budget gap calculated")

    assert dialogue.answer("Events", "Salesforce Park is unavailable. Find another venue.",
                           user_id="U-owner", channel_id="C-demo",
                           thread_ts="100.001", delivery_id="event:venue-change") == "Three venue leads checked"
    assert dialogue.answer("Treasurer", "This is over budget. Suggest a transfer.",
                           user_id="U-owner", channel_id="C-demo",
                           thread_ts="100.001", delivery_id="event:budget-gap") == "Budget gap calculated"
    assert calls == [
        ("venue", {"user_id": "U-owner", "channel_id": "C-demo", "thread_root_ts": "100.001"}),
        ("budget", {"user_id": "U-owner", "channel_id": "C-demo", "thread_root_ts": "100.001",
                    "request_text": "This is over budget. Suggest a transfer."}),
    ]
