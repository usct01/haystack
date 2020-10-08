import logging
import re
from copy import deepcopy
from functools import partial, reduce
from itertools import chain
from typing import List, Optional, Tuple, Generator, Set

import nltk
from more_itertools import windowed

from haystack.preprocessor.base import BasePreProcessor

logger = logging.getLogger(__name__)


class PreProcessor(BasePreProcessor):
    def __init__(
        self,
        clean_whitespace: Optional[bool] = True,
        clean_header_footer: Optional[bool] = False,
        clean_empty_lines: Optional[bool] = True,
        split_by: Optional[str] = "passage",
        split_size: Optional[int] = 10,
        split_stride: Optional[int] = None,
        split_mid_sentence: Optional[bool] = True,
    ):
        """
        :param clean_header_footer: use heuristic to remove footers and headers across different pages by searching
                                     for the longest common string. This heuristic uses exact matches and therefore
                                     works well for footers like "Copyright 2019 by XXX", but won't detect "Page 3 of 4"
                                     or similar.
        :param clean_whitespace: strip whitespaces before or after each line in the text.
        :param clean_empty_lines: remove more than two empty lines in the text.
        :param split_by: split the document by "word", "sentence", or "passage". Set to None to disable splitting.
        :param split_size: n number of splits to merge as a single document. For instance, if n -> 10 & split_by ->
                           "sentence", then each output document will have 10 sentences.
        :param split_stride: overlap splits by "sliding window". Set to None to disable it.
        :param split_mid_sentence: whether to split within sentence.
        :param
        """

        nltk.download('punkt')
        self.clean_whitespace = clean_whitespace
        self.clean_header_footer = clean_header_footer
        self.clean_empty_lines = clean_empty_lines
        self.split_by = split_by
        self.split_size = split_size
        self.split_stride = split_stride
        self.split_mid_sentence = split_mid_sentence

    def clean(self, document: dict) -> dict:
        text = document["text"]
        if self.clean_header_footer:
            cleaned_pages, header, footer = self._find_and_remove_header_footer(
                document, n_chars=300, n_first_pages_to_ignore=1, n_last_pages_to_ignore=1
            )
            logger.debug(f"Removed header '{header}' and footer {footer} in document")

        if self.clean_whitespace:
            lines = text.splitlines()

            cleaned_lines = []
            for line in lines:
                line = line.strip()
                cleaned_lines.append(line)
            text = "\n".join(cleaned_lines)

        if self.clean_empty_lines:
            text = re.sub(r"\n\n+", "\n\n", text)

        document["text"] = text
        return document

    def split(self, document: dict) -> List[dict]:
        if not self.split_by:
            return [document]

        text = document["text"]

        if self.split_mid_sentence:
            if self.split_by == "passage":
                slices = text.split("\n\n")
            elif self.split_by == "sentence":
                slices = nltk.tokenize.sent_tokenize(text)
            elif self.split_by == "word":
                slices = text.split(" ")
            else:
                raise NotImplementedError("PreProcessor only supports 'passage' or 'sentence' split_by options.")

            if self.split_stride:
                segments = windowed(slices, n=self.split_size, step=self.split_size - self.split_stride)
            else:
                segments = windowed(slices, n=self.split_size, step=self.split_size)

            text_splits = []
            for seg in segments:
                txt = " ".join([t for t in seg if t])
                text_splits.append(txt)
        else:
            if self.split_by == "word":
                sentences = nltk.tokenize.sent_tokenize(text)
                word_count = 0
                text_splits = []
                current_slice = ""
                for sen in sentences:
                    current_slice += sen
                    word_count += len(sen.split(" "))
                    if word_count > self.split_size:
                        text_splits.append(current_slice)
                        current_slice = ""
                        word_count = 0
            else:
                raise NotImplementedError

        documents = []
        for i, txt in enumerate(text_splits):
            doc = deepcopy(document)
            doc["text"] = txt
            if "meta" not in doc.keys():
                doc["meta"] = {}
            doc["meta"]["_split_id"] = i
            documents.append(doc)

        return documents

    def _find_and_remove_header_footer(
        self, document: dict, n_chars: int, n_first_pages_to_ignore: int, n_last_pages_to_ignore: int
    ) -> Tuple[List[str], Optional[str], Optional[str]]:
        """
        Heuristic to find footers and headers across different pages by searching for the longest common string.
        For headers we only search in the first n_chars characters (for footer: last n_chars).
        Note: This heuristic uses exact matches and therefore works well for footers like "Copyright 2019 by XXX",
         but won't detect "Page 3 of 4" or similar.

        :param n_chars: number of first/last characters where the header/footer shall be searched in
        :param n_first_pages_to_ignore: number of first pages to ignore (e.g. TOCs often don't contain footer/header)
        :param n_last_pages_to_ignore: number of last pages to ignore
        :return: (cleaned pages, found_header_str, found_footer_str)
        """

        pages = document["text"].split("\f")

        # header
        start_of_pages = [p[:n_chars] for p in pages[n_first_pages_to_ignore:-n_last_pages_to_ignore]]
        found_header = self._find_longest_common_ngram(start_of_pages)
        if found_header:
            pages = [page.replace(found_header, "") for page in pages]

        # footer
        end_of_pages = [p[-n_chars:] for p in pages[n_first_pages_to_ignore:-n_last_pages_to_ignore]]
        found_footer = self._find_longest_common_ngram(end_of_pages)
        if found_footer:
            pages = [page.replace(found_footer, "") for page in pages]
        return pages, found_header, found_footer

    def _ngram(self, seq: str, n: int) -> Generator[str, None, None]:
        """
        Return ngram (of tokens - currently split by whitespace)
        :param seq: str, string from which the ngram shall be created
        :param n: int, n of ngram
        :return: str, ngram as string
        """

        # In order to maintain the original whitespace, but still consider \n and \t for n-gram tokenization,
        # we add a space here and remove it after creation of the ngrams again (see below)
        seq = seq.replace("\n", " \n")
        seq = seq.replace("\t", " \t")

        words = seq.split(" ")
        ngrams = (
            " ".join(words[i : i + n]).replace(" \n", "\n").replace(" \t", "\t") for i in range(0, len(words) - n + 1)
        )

        return ngrams

    def _allngram(self, seq: str, min_ngram: int, max_ngram: int) -> Set[str]:
        lengths = range(min_ngram, max_ngram) if max_ngram else range(min_ngram, len(seq))
        ngrams = map(partial(self._ngram, seq), lengths)
        res = set(chain.from_iterable(ngrams))
        return res

    def _find_longest_common_ngram(
        self, sequences: List[str], max_ngram: int = 30, min_ngram: int = 3
    ) -> Optional[str]:
        """
        Find the longest common ngram across different text sequences (e.g. start of pages).
        Considering all ngrams between the specified range. Helpful for finding footers, headers etc.

        :param sequences: list[str], list of strings that shall be searched for common n_grams
        :param max_ngram: int, maximum length of ngram to consider
        :param min_ngram: minimum length of ngram to consider
        :return: str, common string of all sections
        """
        sequences = [s for s in sequences if s]  # filter empty sequences
        if not sequences:
            return None
        seqs_ngrams = map(partial(self._allngram, min_ngram=min_ngram, max_ngram=max_ngram), sequences)
        intersection = reduce(set.intersection, seqs_ngrams)

        try:
            longest = max(intersection, key=len)
        except ValueError:
            # no common sequence found
            longest = ""
        return longest if longest.strip() else None