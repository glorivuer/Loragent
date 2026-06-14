import logging
from google import genai
from config import GEMINI_API_KEY, ORCHESTRATOR_MODEL
from tools.browser_tool import scrape_finance_news

logger = logging.getLogger(__name__)

async def run(payload: dict) -> str:
    """
    Finance Agent:
    1. Attach to host Chrome over CDP and scrape data from target URL.
    2. Feed raw page content to Gemini for summarization, financial key indicator extraction, and sentiment profiling.
    3. Return markdown financial report.
    """
    url = payload.get("url", "")
    if not url:
        raise ValueError("No url specified for finance agent.")
        
    logger.info(f"Finance Subagent: Starting CDP retrieval for {url}")
    
    # 1. Scrape raw text over CDP
    scraped_data = await scrape_finance_news(url)
    title = scraped_data["title"]
    content = scraped_data["content"]
    
    logger.info(f"Finance Subagent: Analyzing content of '{title}' with Gemini...")
    
    # 2. Invoke Gemini to perform analysis
    client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else genai.Client()
    
    prompt = (
        f"You are a professional financial analyst. Here is raw content fetched from a financial portal:\n"
        f"URL: {url}\n"
        f"Title: {title}\n"
        f"Content:\n{content[:15000]}\n\n" # Truncate to avoid excessive prompt token lengths
        f"Perform a comprehensive financial analysis. Please include:\n"
        f"1. **Executive Summary**: A concise summary of the news/article.\n"
        f"2. **Key Financial Metrics & Facts**: Extract any numbers, dates, stock tickers, values, or metrics.\n"
        f"3. **Market Sentiment**: Analyze whether the news is bullish, bearish, or neutral, with reasons.\n"
        f"4. **Actionable Insights/Impact**: Potential short-term and long-term impact on relevant markets/stocks.\n\n"
        f"Format the output cleanly in markdown format, optimized for Telegram."
    )
    
    response = client.models.generate_content(
        model=ORCHESTRATOR_MODEL,
        contents=prompt
    )
    
    report = response.text
    return (
        f"📰 **Source Title**: {title}\n"
        f"🔗 **URL**: {url}\n\n"
        f"{report}"
    )
