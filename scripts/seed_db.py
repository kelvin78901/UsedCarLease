"""Seed the database with a full pipeline run using the offline seed adapter.
Run this once after install so the API has data to serve before any live crawl:

    python scripts/seed_db.py
"""
from alr.pipeline.run import crawl

if __name__ == "__main__":
    enriched = crawl(adapters=["seed"], persist=True)
    print(f"seeded {len(enriched)} enriched listings into the snapshot.")
