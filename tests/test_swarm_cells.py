from swarm import cells
from swarm.prompts import parse_winner

SRC = "Title\nIn this phase 2 trial of 97 patients, the overall response rate was 88%."
URL = "https://doi.org/10.1000/xyz123"


def problems(text, slot="mechanism", preprint=False):
    return cells.cell_problems(text, source_text=SRC, slot=slot, is_preprint=preprint)


def test_clean_cell_passes():
    assert problems("ORR of 88% in 97 patients is a real signal for the thesis.") == []


def test_invented_number_is_a_problem():
    assert problems("ORR of 90% is strong.") == ["numbers not in the source: 90%"]


def test_length_advice_and_slot_rules():
    assert problems("x" * 281)[0].startswith("281 chars")
    assert "investment advice" in problems("Buy the stock now.")[0]
    assert "medical advice" in problems("Patients should ask their doctor.")[0]
    assert problems("no link here", slot="closer") == []
    assert problems("fine " + URL, slot="closer")[0].startswith("contains a link")
    assert problems("no label", slot="hook", preprint=True) == ["preprint not labelled in the hook"]
    assert problems("a preprint says", slot="hook", preprint=True) == []
    assert problems("   ") == ["empty"]


def test_dedupe_keeps_first_of_near_twins():
    a = "CAR-T durability at 88% ORR resets the bar for the class"
    b = "CAR-T durability at 88% ORR resets the bar for this class"
    c = "A completely different read on the trial design"
    assert cells.dedupe([a, b, c], 0.85) == [a, c]
    assert cells.dedupe([a, b, c], 1.01) == [a, b, c]


def test_tournament_single_elimination_with_bye_and_swapped_order():
    seen = []

    def judge(x, y):
        seen.append((x, y))
        return max(x, y)

    winner, log = cells.tournament(["1", "2", "3", "4", "5"], judge)
    assert winner == "5"
    assert len(log) == 4  # 2 + 1 + 1 matches; "5" had a bye in round one
    # alternate matches present the pair the other way round
    assert seen[0] == ("1", "2") and seen[1] == ("4", "3")


def test_tournament_judge_returning_garbage_falls_back_to_first():
    winner, _ = cells.tournament(["a", "b"], lambda x, y: "zzz")
    assert winner == "a"


def test_parse_winner():
    assert parse_winner('{"winner": "B", "reason": "tighter"}') == "B"
    assert parse_winner('Sure! {"winner":"a"}') == "A"
    assert parse_winner("winner: B") == "B"
    assert parse_winner("both are fine") is None
