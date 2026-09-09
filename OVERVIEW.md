# What this thing actually does

*The version I'd tell a friend over coffee. No code, no jargon. For the
step-by-step commands see HOWTO.md; for how it's built see README.md.*

---

So you know how there's a whole corner of biotech that's about teaching the
immune system to kill cancer? CAR-T, where they engineer a patient's own T
cells. T-cell engagers, which are drugs that drag a T cell over to a tumour
cell and hold them together. Bispecifics, trispecifics, all that. It's one of
the most interesting areas in medicine right now, and it's also where a lot of
money moves: trial results come out, stocks jump or crater, big pharma buys
small companies for billions.

The problem is that following it properly is a full-time job. There are
dozens of company press releases a week, a firehose of papers, conference
abstracts, FDA decisions, and most of it is noise. The good stuff is buried.

This project is my attempt to build a tireless research assistant that reads
all of it so I don't have to, and then helps me write about the stuff that
matters on X. Here's how it works, start to finish.

## 1. It reads everything

Every morning (and on a timer through the day if I want) it goes out and
collects new items from a long list of places: the news feeds of about forty
companies in the space, the big journals, the preprint servers where papers
appear before peer review, the clinical trial registry, the FDA's approvals
page, conference abstract databases, and a list of experts on X who tend to
know things first.

It's polite about it. It asks each source only as often as that source
deserves, it identifies itself to the scientific databases the way they ask
you to, and if a source is down it shrugs and moves on rather than giving up
on the whole run.

## 2. It throws away duplicates and off-topic stuff

The same story usually shows up in three or four places: the company's press
release, a news write-up, the journal paper, a tweet about it. The assistant
notices when different items are really the same story and bundles them into
one, keeping the earliest date.

Then a simple keyword filter asks: is this even about our corner of the
world? A paper about a new statin, or a general oncology guideline, gets
dropped here. Only stories that mention the right kinds of drugs, targets or
companies go forward. There's a daily limit on how many stories get the
expensive treatment in the next step; the extras wait their turn rather than
being thrown away.

## 3. An AI analyst scores each story

This is the heart of it. Each story goes to Claude, the AI, with a very
specific persona: you are a PhD immuno-oncologist who now works as a biotech
analyst at a hedge fund. Read this and score it.

It scores on seven things: is it new, does it matter clinically, would our
audience care, is it in our lane, is it timely, how strong is the evidence,
and how much hype is in the writing. Those combine into a number out of 50.
It also writes a two-line rationale and a suggested angle, like "the
interesting thing here isn't the response rate, it's that they excluded the
patients who'd already had this kind of drug."

The rules I've drilled into it, because it got these wrong early on: never
guess which company or which drug mechanism is involved if the text doesn't
say so. A dated upcoming catalyst for a public company is a big deal even
before there's data. A guideline or a review article with no business angle
is not, however good the science.

## 4. I read the digest and grade its work

Then I get a digest: the top stories, best first, each with its score, the
rationale, and the angle. A second AI, a stronger and more expensive one, has
already rated each entry 1 to 5 as "would we post this?", with a one-line
reason, so I can see where the two models disagree.

Then I rate them myself, 1 to 5, and type a note when I disagree. Those notes
are gold. "This is a 5 because it's a dated catalyst and it explains the
disease model" or "this is a 2, no investment angle at all" are exactly what
I feed back into the scoring rules so it thinks more like me next week. My
ratings are the ground truth; the AI's ratings are just a second opinion.

## 5. It writes drafts

For the stories that score high enough, Claude writes a draft: one standalone
post, plus a short thread of three to six posts that goes deeper. Same
persona, plus a voice guide I wrote: plain English, short sentences, name the
company and its ticker, sceptical of small trials and press-release spin,
always add an interpretation the press release didn't give you. What does
this do to the company's thesis? Who else is chasing the same target and how
far along are they? What does the manufacturing cost structure mean for
margins?

Then the hard rules. Some things are never allowed, and they're checked by
plain code after the AI writes, not by trusting the AI to behave:

- No medical advice. Ever. Describe evidence, never tell anyone what to do
  about their treatment.
- No investment advice. No buy, sell, hold or short, no price targets, no
  promises about returns. Describe what a result means and what the risks
  are; the reader decides.
- Every number in the draft has to appear word-for-word in the source. No
  rounding, no "roughly", no doing the maths yourself.
- Preprints get called preprints.
- The link to the source is always there. Every post fits X's length limit.

A draft that breaks any of these is thrown out and the AI gets another go,
this time told exactly what it did wrong. If it fails four times the story
is set aside for me to look at.

## 6. I approve every single post

Nothing goes out without me. The drafts land in a small web page on my
computer where I read each one and approve it, edit it, reject it, or snooze
it for a day. Each draft comes with a list of "claims to verify": the things
the AI added from its own knowledge rather than from the source, like a
competitor's pipeline stage, so I know what to double-check before I hit
approve.

When I edit, it remembers both versions and why I changed it. Over time those
edits get shown back to the AI as examples of how I actually want things
written, so the drafts drift towards my voice. And once in a while it
produces a report saying "here's what your edits keep asking for; maybe add
these lines to the voice guide," which I read and apply by hand if I agree.

## 7. It posts, carefully

Approved posts go out at set times of day, a few a day at most, spaced out.
Breaking news like an FDA approval can jump the queue. Posting is off by
default and has to be switched on in two separate places before a single
tweet is sent, because the failure mode I'm most afraid of is a bug posting
fifty things at 3am. Every text is checked against the hard rules one more
time right before it goes, and a post is only ever sent once even if the
program runs twice.

## 8. It learns what worked

A week later it looks at how each post did: views, likes, replies, follower
count over time. It cross-references that against the score and the ratings
and suggests changes: "posts about M&A do twice as well as posts about early
science, maybe weight that higher"; "the 7am slot is dead, try 9". It only
proposes. I decide.

## The bits that keep it honest

- **A human approves everything.** The AI never posts on its own.
- **The bio says posts are AI-assisted and nothing is advice.** The
  publisher refuses to run until I've confirmed that's in place.
- **No advice, no made-up numbers, enforced by code**, not by asking nicely.
- **Everything is logged.** Every score, every draft, every edit, every post,
  with which version of the AI and which version of the instructions made it,
  so when something looks off I can trace exactly why.
- **Config, not code.** The list of companies, the keywords, the posting
  times, which AI model does what, all live in plain settings files I can
  read and change without touching the program.

## Why bother

Because the analysts who do this by hand at funds are excellent and
expensive, and the retail investor and the working clinician-scientist don't
get that view. If the assistant can surface the six stories a day that matter
and I can add the interpretation that a press release never will, that's a
useful thing to put into the world. If it grows an audience, great. If it
just makes me a sharper reader of this field, that was worth it too.
