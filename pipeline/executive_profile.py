"""
Scout — output profile constraints.

Injected into the Analyst and Strategist prompts based on `clients.output_profile`.
This is where brevity comes from. Never from lowering max_tokens: that truncates
mid-sentence and breaks JSON parsing, producing an empty week rather than a short one.

Usage in synthesizer.py:

    from executive_profile import profile_block

    analyst_prompt = build_analyst_prompt(...) + profile_block(output_profile, "analyst")
    strategist_prompt = build_strategist_prompt(...) + profile_block(output_profile, "strategist")
"""

# Word caps per field. The validator enforces these after generation, so any change
# here must be mirrored in validate_briefing.py — they are deliberately one dict each
# rather than a shared import, so a drift shows up as a failing week, not a silent pass.
CAPS = {
    "executive": {
        "summary": 75,
        "headline": 10,
        "so_what": 25,
        "recommendation": 30,
        "_default": 40,
    },
    "operator": {},  # internal: uncapped
}

DEV_RANGE = {
    "executive": (2, 4),
    "operator": (0, 99),
}


_ANALYST_EXECUTIVE = """

## OUTPUT PROFILE: EXECUTIVE

This briefing is read by a head of revenue and a CEO. They want the business
implication, not the marketing mechanic. Write to these constraints exactly.

**How many developments.** Between two and four. Never more. The cap exists to force
ranking: they want to know what matters most, not everything that happened. If fewer
than two developments clear the evidence bar, return only what clears it and say so
plainly in the summary. Never pad to reach a number.

**Headlines name the competitor and the verb.** Ten words maximum, and specific.
  GOOD: "Movement opened a second Crystal City location"
  BAD:  "Competitive activity increased in the DC market"
A headline that could describe any week in any market is filler. Rewrite it.

**Word caps, strictly enforced downstream.** A briefing that exceeds these is rejected
and held rather than published, so write to them rather than past them:
  summary          75 words
  headline         10 words
  so_what          25 words
  recommendation   30 words
  every other text field  40 words

**No blocks of text.** Every field is a discrete element. If an idea needs a paragraph,
it needs a structure instead. Nothing runs past 40 words.

**The summary never opens generically.** Do not begin with "This week saw continued
competitive activity," "Overall," "In summary," or any variant. If nothing moved, say
nothing moved — a quiet week reported honestly is more useful than a manufactured one.

**The evidence bar tightens under compression, it does not loosen.** A ten-word headline
cannot carry the hedge a paragraph can, so:
  - Every claim must trace to signals collected this week. No claim without a signal.
  - Any development supported by a single signal is OMITTED, not compressed into a
    confident-sounding headline. Suppression is correct behaviour; guessing is not.
  - State what was observed, not what it means as fact. "Published three posts about
    youth programs" is an observation. "Is pivoting to a youth strategy" is an inference
    and belongs in the recommendation, labelled as inference.
  - Every development carries the signal IDs it rests on, and a source URL.

**Constructions that are rejected automatically.** Do not use any of these:
  - Em dashes. Use commas or full stops.
  - "Here's what / here's the thing / it turns out / the truth is / the reality is /
    it's worth noting / at the end of the day / make no mistake / let that sink in."
  - Two-beat antithesis: "It's not X, it's Y." State Y directly.
  - Aphorisms and pull-quote lines. If a sentence sounds quotable, rewrite it.
  - Adverbs: really, just, actually, simply, genuinely, truly, significantly, notably.
"""


_STRATEGIST_EXECUTIVE = """

## OUTPUT PROFILE: EXECUTIVE

**One recommended action per development.** Not a list of options. If two actions are
genuinely required, they belong to two developments or to one action with a sequence.

**30 words maximum per recommendation.** The observation field is a brief reference
under 20 words that gives context, never a restatement of what the Analyst already said.

**Tie every recommendation to a business outcome the reader owns** — conversion of
existing traffic, new-member acquisition economics, market position, or the narrative
problem. A recommendation whose payoff is "better marketing" has not been finished.

**Respect the constraints in the client context.** A recommendation the client cannot
act on is worse than no recommendation, because it signals we did not read their
situation. Check the client's stated constraints before proposing anything that requires
significant development work, replaces human interaction with automation, or assumes
resources they have said they lack.

**Label inference as inference.** Where a recommendation rests on reading intent into an
observation, say so in the recommendation itself. Do not launder an inference into the
observation field.

**Same rejected constructions as the Analyst.** No em dashes, no throat-clearing, no
two-beat antithesis, no aphorisms, no adverbs.
"""


_OPERATOR = """

## OUTPUT PROFILE: OPERATOR

Internal Parallel Path readers. Full detail, no word caps, no development cap. Include
low-confidence items with their confidence level stated rather than suppressing them —
the reader is a strategist who can weigh them.

Still no em dashes, no throat-clearing, no two-beat antithesis, no adverbs. Those rules
are about writing quality and apply to every audience.
"""


def profile_block(output_profile: str, role: str) -> str:
    """Return the prompt constraints for a profile and role.

    role: "analyst" | "strategist"
    output_profile: "executive" | "operator"
    Unknown profiles fall back to operator, which is the permissive one — a bad config
    value should not silently impose caps nobody asked for.
    """
    if output_profile != "executive":
        return _OPERATOR
    return _ANALYST_EXECUTIVE if role == "analyst" else _STRATEGIST_EXECUTIVE
