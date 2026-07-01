"""Agents — each a single-responsibility unit in the crawl pipeline.

navigator  -> discovers subcategory listing pages and product URLs
classifier -> decides page type (listing / product / other)
extractor  -> pulls normalized fields (rules-first, LLM fallback)
validator  -> validates + deduplicates before persistence
"""
