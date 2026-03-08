UV := uv
PROJECT := .

APP_MODULE := loveletter.main:app
HOST ?= 0.0.0.0
PORT ?= 8000

SIM_GAMES ?= 200
SIM_PLAYER_COUNTS ?= 2 3 4 5 6
SIM_BASE_SEED ?= 20260308

.PHONY: run format test

run:
	$(UV) run --project $(PROJECT) uvicorn $(APP_MODULE) --host $(HOST) --port $(PORT) --reload

format:
	$(UV) run --project $(PROJECT) ruff check --select I --fix .
	$(UV) run --project $(PROJECT) ruff format .

test:
	$(UV) run --project $(PROJECT) python -m py_compile \
		loveletter/main.py \
		loveletter/game_logic.py \
		loveletter/simulate_random_agents.py
	$(UV) run --project $(PROJECT) python -m loveletter.simulate_random_agents \
		--games-per-config $(SIM_GAMES) \
		--player-counts $(SIM_PLAYER_COUNTS) \
		--base-seed $(SIM_BASE_SEED) \
		--randomize-first-player
