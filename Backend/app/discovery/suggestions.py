"""Optional research hypotheses; suggestions do not affect trend assessment."""

from app.discovery.contracts import Candidate, Suggestion


def suggest_category(candidate: Candidate) -> Suggestion:
    categories = set(candidate.categories)
    if "fashion" in categories:
        return Suggestion("SUGGESTION_FOR_RESEARCH", "fashion",
                          "Keyword matches an explicit fashion vocabulary rule; verify independently.",
                          "LOW")
    if "home_living" in categories:
        return Suggestion("SUGGESTION_FOR_RESEARCH", "home_living",
                          "Keyword matches an explicit home vocabulary rule; verify independently.",
                          "LOW")
    return Suggestion("NO_PRODUCT_SUGGESTION", None,
                      "No supported product category evidence.", "LOW")
