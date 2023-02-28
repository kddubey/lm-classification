'''
Helpers for interacting with the OpenAI API:
  - automatically retry and sleep if requests fail
  - automatically batch requests for greater efficiency 
'''
from __future__ import annotations
import logging
import os
import time
from typing import Any, Callable, Literal, Mapping, Sequence, get_args

import openai
from tqdm.auto import tqdm
from transformers import GPT2Tokenizer

from lm_classification.utils import batch


logger = logging.getLogger(__name__)


openai.api_key = os.getenv('OPENAI_API_KEY')


gpt2_tokenizer: GPT2Tokenizer = GPT2Tokenizer.from_pretrained('gpt2')
## TODO: I'm not sure whether GPT2Tokenizer or GPT2TokenizerFast is used for the
## text models in the OpenAI API


Model = Literal['text-ada-001',
                'text-babbage-001',
                'text-curie-001',
                'text-davinci-003']
_costs = [0.0004, 0.0005, 0.002, 0.02]
## https://openai.com/api/pricing/
## TODO: figure out how to get this automatically from openai, if possible
model_to_cost_per_1k: dict[Model, float] = dict(zip(get_args(Model), _costs))


def openai_method_retry(openai_method: Callable, max_num_tries: int=5,
                        sleep_sec: float=10, **openai_method_kwargs):
    '''
    Returns `openai_method(**openai_method_kwargs)`, retrying up to
    `max_num_tries` times and sleeping `sleep_sec` in between tries if the call
    raises `openai.error.ServiceUnavailableError` or
    `openai.error.RateLimitError`.
    '''
    num_tries = 0
    while num_tries < max_num_tries:
        try:
            return openai_method(**openai_method_kwargs)
        except (openai.error.ServiceUnavailableError,
                openai.error.RateLimitError) as e:
            num_tries += 1
            logger.info(f'openai error: {e}')
            logger.info(f'Try {num_tries}. Sleeping for {sleep_sec} sec.')
            time.sleep(sleep_sec)
            exception = e ## allow it to be referenced later
    logger.error(f'Max retries exceeded. openai error: {exception}')
    raise exception


class UserCanceled(Exception):
    pass


def _openai_api_call_is_ok(model: Model, texts: list[str], max_tokens: int=0):
    '''
    Prompts the user to input `y` or `n`, given info about cost. Raises
    `UserCanceled` if the user inputs `n` to the prompt.
    '''
    texts = list(texts)
    _num_tokens_prompts = sum(len(tokens) for tokens in
                              gpt2_tokenizer(texts)['input_ids'])
    _num_tokens_completions = len(texts) * max_tokens ## upper bound ofc
    num_tokens = _num_tokens_prompts + _num_tokens_completions
    cost_per_1k_tokens = model_to_cost_per_1k.get(model)
    if cost_per_1k_tokens is None:
        cost = 'unknown'
    else:
        cost = round(num_tokens * cost_per_1k_tokens / 1_000, 2)
    output = None
    while output not in {'y', 'n'}:
        output = input(f'This API call will cost you about ${cost} '
                       f'({num_tokens:_} tokens). Proceed? (y/n): ')
    if output == 'n':
        raise UserCanceled


def gpt3_complete(texts: Sequence[str], model: Model, ask_if_ok: bool=False,
                  max_tokens: int=0,
                  **openai_completion_kwargs) -> list[Mapping[str, Any]]:
    '''
    Returns a list `choices` where `choices[i]` is the value of
    `'choices'` (from the OpenAI Completion endpoint) for `texts[i]` using
    `model`.

    #### Note that, by default, `max_tokens` to generate is 0!

    If `ask_if_ok`, then you'll be notified of the cost of this call, and then
    prompted to give the go-ahead.
    '''
    _batch_size = 20 ## max that the API can currently handle
    if isinstance(texts, str):
        ## Passing in a string will silently but majorly fail. Handle it
        texts = [texts]
    if ask_if_ok:
        _openai_api_call_is_ok(model, texts, max_tokens=max_tokens)
    choices = []
    with tqdm(total=len(texts), desc='Computing probs') as progress_bar:
        for texts_batch in batch.constant(texts, _batch_size):
            response = openai_method_retry(openai.Completion.create,
                                           prompt=texts_batch,
                                           model=model,
                                           max_tokens=max_tokens,
                                           **openai_completion_kwargs)
            choices.extend(response['choices'])
            progress_bar.update(len(texts_batch))
    return choices