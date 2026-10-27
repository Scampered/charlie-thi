import re
from urllib.parse import unquote, urlparse

import scrapy


class CaifirstbotSpider(scrapy.Spider):
    name = "caifirstbot"
    # Pages scraped when no url=... is given. Add more Wikipedia links here.
    start_urls = [
        "https://en.wikipedia.org/wiki/Ingolstadt",
        "https://en.wikipedia.org/wiki/Technische_Hochschule_Ingolstadt",
    ]

    def __init__(self, url=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Optional, one or more links separated by spaces or commas:
        #   scrapy crawl caifirstbot -a url="https://en.wikipedia.org/wiki/Munich https://en.wikipedia.org/wiki/Audi"
        if url:
            self.start_urls = [u for u in re.split(r"[\s,]+", url) if u]

    def parse(self, response):
        # Name of the page, e.g. "FC Ingolstadt 04" - saved with every item so Charlie knows where a sentence came from
        source = unquote(urlparse(response.url).path.rsplit("/", 1)[-1]).replace("_", " ")

        # h2 = category, h3 = sub-category, p = text. Junk sections (References, See also, ...)
        # are skipped later in prepare_data.py, together with all text under them.
        for element in response.css(".mw-parser-output h2, .mw-parser-output h3, .mw-parser-output p"):
            tag = element.root.tag
            text = "".join(element.css("::text").getall()).strip()

            #remove css
            text = re.sub(r"\.mw-parser-output [^{]+\{[^}]+\}", "", text)

            #remove refferences
            text = re.sub(r"\[\d+\]|\[[a-z]\]|\[citation needed\]", "", text)

            text = re.sub(r"\s+", " ", text).strip()

            if text:
                yield {
                    "tag": tag,
                    "text": text,
                    "source": source,
                }
