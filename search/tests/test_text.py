from search.text import best_quote, decompound, deep_link, sentences, tokenize


def test_tokenize_languages_meet():
    assert tokenize("Glucose Sensors") == tokenize("glucose sensor")
    assert tokenize("Sensoren")[0] == tokenize("sensor")[0]
    assert tokenize("Implantate") == tokenize("Implantat")
    assert tokenize("Hörimplantat") == tokenize("horimplantat")
    assert "der" not in tokenize("der Sensor und die Pumpe")


def test_decompound():
    vocab = {"knoch", "implantat"}
    assert decompound("knochenimplantat", vocab) == ["knoch", "implantat"]
    assert decompound("short", vocab) == []


def test_quote_is_verbatim():
    text = "# Title\n\n- We build continuous glucose sensors for people with diabetes.\n- Phone 031"
    q = best_quote(text, set(tokenize("glucose sensor")))
    assert q in text
    assert "glucose" in q
    for s in sentences(text):
        assert s in text


def test_quote_truncation_verbatim():
    text = " ".join(f"word{i}" for i in range(100)) + " glucose."
    q = best_quote(text, {"glucos"}, max_words=10)
    assert q in text and len(q.split()) == 10


def test_deep_link_encoding():
    u = deep_link("https://x.ch/a#old", "Co-Head, Otology & Hearing")
    assert u.startswith("https://x.ch/a#:~:text=")
    frag = u.split("#:~:text=")[1]
    assert "-" not in frag and "," not in frag and "&" not in frag
    long = deep_link("https://x.ch/a", " ".join(f"w{i}" for i in range(20)))
    assert long.count(",") == 1
