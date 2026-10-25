"""
Whether the customer said yes, decided in code (review finding D1).

A confirmation from the model (customer_confirmed=true) counts only when the customer's own message in this turn, as
the channel gateway received it (sly_data customer_message, gateway_auth.sign), is a clear yes: it starts with a yes
and has nothing that takes it back or makes it conditional. Anything else ("no thanks", "maybe", "yes but only the
shoes", "yes?") is not a yes, and the customer is asked again. A false "not a yes" costs one question; a false "yes"
could cancel an order, so the rule leans to asking again.

Used by action_layer.confirmation_accepted (actions that need the customer's confirmation) and by
companion_tools.RememberPreference (remembering a preference).
"""

import re
from typing import Any

from coded_tools.customer_service.gateway_auth import CUSTOMER_MESSAGE

MAX_CHARS = 300  # a longer message is more than an answer to the question: ask again
_LEAD = r"(?:(?:oh|ah|um+|hi|hello|great|thanks|thank you|perfect|lovely)[\s,.!]+)*"
YES = re.compile(_LEAD + r"(?:yes|yeah|yep|yup|ya|sure|ok|okay|alright|all right|please do|please go ahead|go ahead|"
                         r"do it|do that|confirm(?:ed)?|i confirm|i agree|agreed|correct|that'?s (?:fine|right|ok|"
                         r"okay|good|great)|sounds good|absolutely|definitely|of course|please)\b", re.I)
# Anything that takes the yes back, limits it or asks something: not a clear yes.
NOT_CLEAR = re.compile(r"\?|\b(?:no|not|nope|nah|don'?t|do not|doesn'?t|never|stop|wait|hold on|hang on|rather not|"
                       r"instead|but|except|unless|only if|maybe|perhaps|not sure|unsure|later|cancel that|"
                       r"changed my mind)\b|n't\b", re.I)


def is_clear_yes(text: str | None) -> bool:
    message = " ".join(str(text or "").split())
    if not message or len(message) > MAX_CHARS:
        return False
    return bool(YES.match(message)) and not NOT_CLEAR.search(message)


def answer_refusal(sly_data: dict[str, Any]) -> str:
    """'' if the customer's message in this turn is a clear yes, else why a confirmation does not count."""
    if CUSTOMER_MESSAGE not in sly_data:
        return "the customer's message was not passed on by the channel, so their answer cannot be checked"
    if not is_clear_yes(sly_data[CUSTOMER_MESSAGE]):
        return "the customer's message is not a clear yes; ask them again, in one short question"
    return ""
