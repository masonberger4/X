"""Where a post cites a price target (draft/targets.py): the phrases, the figures written
next to the word target, and the many other targets of this account's news (an antigen, a
target lesion, a revenue or EPS target, a takeover target's deal price) that are not price
targets."""

from __future__ import annotations

import pytest

from draft.targets import field_figure, target_figures, target_mentions, target_problems

IOVANCE = (
    "The stock closed at $14.45, up 31.5%. H.C. Wainwright raised its target to $20 from $9, "
    "and Wells Fargo to $18 from $14 the next day; Goldman had put a Buy and a $15 target on "
    "it five days earlier. At Friday's close of $14.22 the company is worth about $6.4B, and "
    "the shares sit above the average analyst target (~$12.40 on MarketBeat's tally)."
)


def test_every_target_in_a_real_passage_and_no_share_price():
    assert target_figures(IOVANCE) == ["20", "9", "18", "14", "15", "12.40"]
    assert target_mentions(IOVANCE) == [
        "target to $20",
        "$15 target",
        "average analyst target (~$12.40",
    ]


# Analysts' targets as the press covers them and as an analyst would write them, with the
# figures each gives as targets.
CITATIONS = [
    # sell-side moves as covered by TheFly, Reuters, MarketBeat, Benzinga, Investing.com
    ("H.C. Wainwright raised its price target on Iovance to $20 from $9.", ["20", "9"]),
    ("Jefferies lowered its price target on Arcellx to $85 from $96.", ["85", "96"]),
    ("Iovance price target raised to $20 from $9 at H.C. Wainwright.", ["20", "9"]),
    (
        "H.C. Wainwright raised their price target on shares of Iovance Biotherapeutics from "
        "$9.00 to $20.00 and gave the company a buy rating.",
        ["9.00", "20.00"],
    ),
    ("Analyst Joseph Pantginis set a price target of $20.", ["20"]),
    ("The average price target of $13.40 implies a 7.2% downside.", ["13.40"]),
    ("It has a consensus target price of $12.40.", ["12.40"]),
    ("MarketBeat's consensus price target is $12.40.", ["12.40"]),
    ("Jefferies set a price target of $40.", ["40"]),
    ("Guggenheim cut its price target to $25 after the interim look.", ["25"]),
    ("Shares at $14 trade above the $12.40 consensus target.", ["12.40"]),
    # the same moves without "price"
    ("Wainwright's target went to $20 from $9 after the label expansion.", ["20", "9"]),
    ("Its target is now $20, up from $9.", ["20", "9"]),
    ("The firm's target was raised to $20 from $9.", ["20", "9"]),
    ("Leerink cut its target on shares of Allogene to $4 from $7.", ["4", "7"]),
    ("TD Cowen raised its target on Arcellx to $100.", ["100"]),
    ("Truist trimmed its Legend Biotech target to $70.", ["70"]),
    ("Oppenheimer's target goes from $30 to $45 on the TIL data.", ["30", "45"]),
    ("Jefferies' target rose to $40 after the readout.", ["40"]),
    ("Jefferies set its target at a Street-high $50.", ["50"]),
    ("Its target of just $8 assumes the melanoma label alone.", ["8"]),
    ("Jefferies' target, $40, assumes peak Amtagvi sales of $1.2B.", ["40"]),
    ("Jefferies' price target, now $40, values NSCLC at 40% odds.", ["40"]),
    ("Stifel's $38 target (cut from $45) values only NSCLC.", ["38", "45"]),
    ("Stifel's $38 target, and Guggenheim at $40, both leave out NSCLC.", ["38", "40"]),
    ("The target price of ~$22 is H.C. Wainwright's.", ["22"]),
    ("The Street-high target of $50 belongs to Jefferies.", ["50"]),
    ("BMO's $120 Street-low price target on Moderna assumes the vaccine fails.", ["120"]),
    ("Of 14 analysts, 11 rate it a Buy; the median target is $32.", ["32"]),
    ("Wall Street's average target is $12.40.", ["12.40"]),
    ("Analysts' targets range from $8 to $30, per MarketBeat.", ["8", "30"]),
    ("Targets run from $8 to $30 across the 12 analysts covering it.", ["8", "30"]),
    ("H.C. Wainwright and Wells Fargo raised targets to $20 and $18.", ["20", "18"]),
    ("Targets of $20 (H.C. Wainwright) and $18 (Wells Fargo) both moved.", ["20", "18"]),
    ("Cantor sees a $20-$25 target range depending on the NSCLC label.", ["20", "25"]),
    # PT and the other abbreviations
    ("Stifel: Buy, PT $38 (from $45). Guggenheim $38 PT.", ["38", "45"]),
    ("Guggenheim PT raised to $40 from $33.", ["40", "33"]),
    ("$IOVA PT cut to $12 at Piper.", ["12"]),
    ("Stifel PT: $52", ["52"]),
    ("BofA PO $45.", ["45"]),
    ("Citi TP HK$130.", ["130"]),
    # other words for a target
    ("BofA raised its price objective on Summit to $45.", ["45"]),
    ("Morningstar's fair value estimate for BioNTech is $121.", ["121"]),
    ("Morningstar's fair value estimate of $60 assumes a 2028 launch.", ["60"]),
    ("Morningstar keeps a $121 fair-value estimate on BioNTech.", ["121"]),
    ("That puts fair value near $30.", ["30"]),
    ("Mizuho's $30 base-case target assumes 35% odds in NSCLC.", ["30"]),
    ("Stifel's $38-per-share target values only the melanoma label.", ["38"]),
    ("Its $38 12-month price target assumes a 2028 launch.", ["38"]),
    ("Stifel price target: $52", ["52"]),
    # four figures, and a listing's own currency
    ("Leerink raised its target on Regeneron to $1,050 from $980.", ["1050", "980"]),
    ("Evercore's $1,100 target on Lilly assumes orforglipron peaks above $10B.", ["1100"]),
    ("Citi lifted its target price for Akeso to HK$130 from HK$110.", ["130", "110"]),
    ("Citi's target on Akeso is HK$130.", ["130"]),
    ("Danske Bank raised its Genmab target to DKK 2,400.", ["2400"]),
    ("Kepler Cheuvreux cut its argenx target to EUR 650.", ["650"]),
    ("Kepler Cheuvreux cut its argenx target to €650.", ["650"]),
    ("UBS has a CHF 310 target on Roche.", ["310"]),
    ("Jefferies keeps a £18 target on GSK.", ["18"]),
    ("Its 12-month target of US$45 assumes approval in 2027.", ["45"]),
    # a fact base's shorthand, which a session may copy into a post
    ("Stifel Buy $38 (from $45), Guggenheim Buy $38 (from $40).", ["38", "45", "40"]),
    ("H.C. Wainwright Neutral at $23 (from $30).", ["23", "30"]),
    ("Goldman Buy, $41.", ["41"]),
    ("Upgraded to Outperform with a $30 target.", ["30"]),
    ("Consensus ~$28.64.", ["28.64"]),
    ("Consensus sits near $28.64.", ["28.64"]),
    # cited without a figure
    ("Shares trade 15% above the consensus target.", []),
    ("At $46 a share, the offer is 12% above the average price target.", []),
    ("Shares sit 40% below the Street's average target.", []),
    ("Goldman's target implies 70% upside even without NSCLC.", []),
]


@pytest.mark.parametrize("text, figures", CITATIONS)
def test_the_citations_and_the_figures_they_give_as_targets(text, figures):
    assert target_figures(text) == figures
    assert target_mentions(text)
    assert target_problems(text)


# What this account's news calls a target, or prices next to the word, without citing an
# analyst's price target.
NOT_CITATIONS = [
    # biology: antigens, RECIST target lesions, PK target occupancy
    "Ivonescimab targets PD-1 and VEGF.",
    "The CD19 target is lost in about a third of relapses.",
    "Its target, CLDN18.2, is expressed in about 40% of gastric cancers.",
    "Median target lesion shrinkage was 38% in the 2 mg/kg cohort.",
    "Mean target occupancy stayed above 90% at doses of 3 mg/kg and up.",
    "The average target lesion reduction was 45% in responders.",
    "At the highest target dose level, two DLTs were seen.",
    "Responses clustered in patients with the highest target expression.",
    "Dosing every three weeks should mean target coverage holds between infusions.",
    "A clean readout would mean target validation for the whole class.",
    "The consensus target sequence for the guide RNA was conserved.",
    "On-target toxicity was low; the targeted therapy lists at $450K.",
    "The target population is 780 patients, with target enrollment of 300.",
    "The target product profile calls for an ORR above 40%.",
    "Target expression above 50% was required, at a list price of $450,000.",
    "With target engagement at 95%, the $20 share price looks low.",
    "The CD19 target on B cells is shed at relapse.",
    # regulators and timing
    "The PDUFA target action date is December 12, 2026.",
    "With a PDUFA target action date of Feb. 28 and $410M in cash, runway lasts into 2028.",
    "The stock fell 12% to $6.20 after the FDA missed its PDUFA target date.",
    "Enrollment target on track and the stock at $14.",
    # a company's revenue, sales, cost, EPS and margin targets
    "It is chasing a $5B target market.",
    "A 2027 revenue target of $500M, or $2 billion by 2030.",
    "Revenue guidance went to $410-420M; the target is profitability.",
    "Management set a 2030 sales target of $3 to $4 billion for Amtagvi.",
    "Iovance cut its 2026 revenue target to $250 to $300 million.",
    "The company raised its cost-savings target to $1.5 bln.",
    "Bayer's revenue target for 2026 is EUR 1.5 billion.",
    "Its sales target for Amtagvi is $1.2 billion by 2028.",
    "The company targets $2 billion in Amtagvi sales by 2030.",
    "The company targets $10.50 in 2028 EPS.",
    "Merck's 2028 EPS target of $10.50 assumes Keytruda Qlex conversion.",
    "The company lowered its non-GAAP EPS target to $8.93.",
    "Manufacturing cost per dose has a $35 target in the 2027 plan.",
    "A CAR-T list price of $465,000, against a cost target of $50,000 per patient.",
    "Sales of $70M, vs. a $65M target.",
    # deals: the takeover target and its price
    "Merck will pay $46 a share in cash, a 75% premium.",
    "The tender offer at $46 per share in cash expires on Nov. 14.",
    "Merck's bid values the target at $46 a share.",
    "As a takeover target at $46 a share, Verona trades on the spread.",
    "Merck offered the target $46 per share in cash plus a CVR.",
    "The bid for the takeover target was $46 a share.",
    "The tender offer for the target at $46 per share in cash runs to Nov. 14.",
    "At $46 target shareholders get a 75% premium to Friday's close.",
    "Shares of the target rose to $45.80 on the news.",
    "Merck values the target company at $46 a share.",
    "The deal price of $97 a share is above the target's 52-week high of $71.",
    # accounting fair values, a CVR's and a warrant's
    "The deal's fair value of $45M was booked as goodwill.",
    "Each CVR carries a fair value of $2.10 per right.",
    "Warrants with a fair value of $0.85 each were issued with the shares.",
    "Analysts put the CVR's fair value at $0.80 a share.",
    # financings, share prices and time zones
    "The offering priced at $18.36 per share, a 7% discount.",
    "The stock closed at $14.45 after the readout.",
    "A webcast starts at 8:00 a.m. PT.",
    "The call is at 4:30 p.m. ET / 1:30 p.m. PT; $IOVA closed at $14.45.",
    "Pts on the PT arm.",  # "PT" counts only with a figure
    "Patients took 10 mg PO daily; $IOVA closed at $14.45.",
    "TP53 mutations were seen in 40% of patients.",
    "Buy-side interest picked up; the stock is at $14.",
    "In a Neutral-rated name at $14, the readout is the catalyst.",
    # results set against the Street, and an EPS consensus
    "Q3 Amtagvi revenue of $71M topped Street targets.",
    "Keytruda sales of $8.1B topped the consensus target of $7.9B.",
    "EPS of $2.58 vs consensus $2.35 on Keytruda strength.",
    "Merck earned $2.58 a share, above the consensus $2.35.",
    "The consensus $2.35 EPS looks stale.",
    # a drug's price
    "ICER's value-based target price of $150,000 to $300,000 would make it cost-effective.",
    "Iovance's target price for Amtagvi in Europe is about $500K.",
    "Amtagvi's list price of $515,000 and a target price for Europe of EUR 350,000.",
    # a link's path
    "Per stocktwits.com/news-articles/markets/smmt-price-target-harmoni-data/cZZp the date holds.",
    "Source: https://example.com/iovance-price-target-raised-to-20/",
    # targets that are not prices at all
    "Keytruda's target and Avastin's target overlap.",
    "G12D is a hard target; CTLA-4 was never the wrong target.",
]


@pytest.mark.parametrize("text", NOT_CITATIONS)
def test_biotech_targets_deals_and_other_dollars_are_not_price_targets(text):
    assert target_mentions(text) == []
    assert target_figures(text) == []
    assert target_problems(text) == []


@pytest.mark.parametrize(
    "text, figures",
    [
        # a share price or a market value set against a target is not a target
        ("Stifel's $38 target vs $14 today implies 170% upside.", ["38"]),
        ("Stifel's $38 target vs $1.2B in cash is the whole debate.", ["38"]),
        ("A $38 PT vs $1.2B market cap.", ["38"]),
        ("The consensus target of $28.64 vs $15.48 at the close assumes a coin flip.", ["28.64"]),
        ("Guggenheim's $38 target (vs $18.36 paid by AstraZeneca) values first line.", ["38"]),
        ("The stock closed at $14.45 and H.C. Wainwright raised its target to $20.", ["20"]),
        ("PT $38, from $5B peak sales.", ["38"]),
    ],
)
def test_only_the_target_figures_count(text, figures):
    assert target_figures(text) == figures


def test_a_mention_inside_a_longer_one_is_named_once():
    assert target_mentions("Jefferies set a price target of $40.") == ["price target of $40"]


def test_the_drafter_cites_no_target():
    [problem] = target_problems("H.C. Wainwright raised its target to $20 from $9.")
    assert problem.startswith("cites a price target ('target to $20')")
    assert "a thread cannot say what a target rests on" in problem


@pytest.mark.parametrize(
    "field, figure",
    [
        ("$20", "20"),
        ("$20 (note of 2026-09-30)", "20"),
        ("12-month $38", "38"),
        ("from $9", "9"),
        ("$1,200", "1200"),
        ("HK$130", "130"),
        ("€45", "45"),
        ("EUR 650", "650"),
        ("45 USD", "45"),
        ("38.00 dollars", "38.00"),
        ("twenty", None),
        ("", None),
    ],
)
def test_the_figure_a_piece_json_field_gives(field, figure):
    assert field_figure(field) == figure
