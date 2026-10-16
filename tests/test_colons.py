from studio.colons import colon_problems, stray_colons


def test_a_colon_before_bullets_or_numbered_lines_is_fine():
    text = (
        "Three things to watch:\n\n• the FDA decision\n• the 12-month data\n\n"
        "Why:\n1. dose\n2. price"
    )
    assert stray_colons(text) == []


def test_a_colon_before_an_inline_list_is_fine():
    assert stray_colons("Three risks: dose, durability and price.") == []
    assert stray_colons("Two readouts: lung or kidney first.") == []
    assert stray_colons("Sources: Merck, the FDA, ESMO") == []


def test_times_ratios_and_links_are_not_colons():
    assert stray_colons("The call is at 8:30 ET, randomised 1:1, on 4:5 cards.") == []
    assert stray_colons("see https://example.com") == []


def test_a_colon_with_no_list_after_it_is_flagged():
    assert stray_colons("My read: the market is pricing the wrong risk.") == [
        "My read: the market is pricing the wrong risk."
    ]
    assert len(stray_colons("THE CATCH:\n\nThe control arm was weak.")) == 1
    assert len(stray_colons("• Mid-2027: the kidney readout")) == 1
    # Only the colon's own sentence counts as the list it might start.
    assert len(stray_colons("The catch: it is small. Dose, durability and price matter.")) == 1


def test_colon_problems_names_the_label_and_caps_the_examples():
    text = "\n".join(f"Point {k}: it matters." for k in range(5))
    (problem,) = colon_problems(text, "post 1")
    assert problem.startswith("post 1 uses a colon where no list follows")
    assert "and 2 more" in problem
    assert colon_problems("Fine. No colon here.", "post 1") == []
