"""KAP Feed data source wrapper.

Retrieves recent disclosures for a stock ticker and extracts the clean Turkish text.
Uses the underlying borsapy KAPProvider.
"""

import logging
import re
from datetime import datetime
from bs4 import BeautifulSoup
from borsapy._providers.kap import get_kap_provider

logger = logging.getLogger(__name__)

class KAPFeed:
    """Wrapper around KAPProvider to fetch and parse disclosures for specific tickers."""

    def __init__(self):
        self.provider = get_kap_provider()

    def get_latest_disclosures(self, ticker: str, limit: int = 5) -> list[dict]:
        """Fetch latest disclosures for a ticker.

        Returns:
            List of dicts: [{
                'date': datetime,
                'title': str,
                'url': str,
                'id': str,
                'ticker': str
            }]
        """
        ticker = ticker.upper().replace(".IS", "").strip()
        try:
            df = self.provider.get_disclosures(ticker, limit=limit)
            if df.empty:
                return []

            disclosures = []
            for _, row in df.iterrows():
                date_str = row['Date']
                try:
                    dt = datetime.strptime(date_str, "%d.%m.%Y %H:%M:%S")
                except ValueError:
                    dt = datetime.now()

                url = row['URL']
                # Extract ID from URL
                match = re.search(r'/Bildirim/(\d+)', url)
                disc_id = match.group(1) if match else url.split('/')[-1]

                disclosures.append({
                    'date': dt,
                    'title': row['Title'],
                    'url': url,
                    'id': disc_id,
                    'ticker': ticker
                })
            return disclosures
        except Exception as e:
            logger.error("Failed to fetch disclosures for %s: %s", ticker, e)
            return []

    def get_disclosure_text(self, disclosure_id: str) -> str:
        """Fetch and clean the Turkish text of a disclosure, removing English translations and noise."""
        try:
            html = self.provider.get_disclosure_content(disclosure_id)
            if not html:
                return ""

            soup = BeautifulSoup(html, 'html.parser')

            # Decompose English elements to filter them out
            for el in soup.select('[class*="content-en"]'):
                el.decompose()

            text = soup.get_text('\n')
            clean_text = re.sub(r'\n+', '\n', text)
            lines = [line.strip() for line in clean_text.split('\n') if len(line.strip()) > 0]

            # Find where the relevant company metadata starts
            start_idx = 0
            for i, line in enumerate(lines):
                if any(k in line for k in ["Gönderim Tarihi", "Bildirim Tipi", "Özet Bilgi", "Açıklamalar"]):
                    start_idx = i
                    break

            # Filter out footer noise and only keep the body
            meaningful_lines = []
            for line in lines[start_idx:]:
                if any(k in line for k in ["Copyright ©", "Tüm Hakları Saklıdır", "YAZDIR", "PDF", "WORD", "EXCEL"]):
                    if len(meaningful_lines) > 5:  # stop if we gathered enough text
                        break
                meaningful_lines.append(line)

            return "\n".join(meaningful_lines)
        except Exception as e:
            logger.error("Failed to get disclosure content for %s: %s", disclosure_id, e)
            return ""
