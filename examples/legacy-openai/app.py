"""Representative migration fixture; no provider call runs at import time."""

import openai


def answer(model, messages):
    """Extract the first chat response's text."""
    response = openai.ChatCompletion.create(
        model=model,
        messages=messages,
        temperature=0,
    )
    return response["choices"][0]["message"]["content"]
