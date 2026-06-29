.PHONY: install seed train api crawl schedule up down clean

install:        ## install the package + deps (editable)
	pip install -e .

seed:           ## seed the DB via the offline pipeline
	python scripts/seed_db.py

train:          ## train the LambdaMART ranker from the snapshot
	python scripts/train_ltr.py

api:            ## run the API + dashboard at http://localhost:8000
	uvicorn alr.api.main:app --reload --port 8000

crawl:          ## run one crawl using ALR_ADAPTERS (default: config)
	python -m alr.pipeline.run

schedule:       ## run the standalone crawl scheduler (separate store only)
	python -m alr.scheduler

up:             ## docker compose up
	docker compose up --build

down:
	docker compose down

clean:          ## wipe local data/model
	rm -f data/autoleaserank.duckdb data/ltr_lambdamart.txt
