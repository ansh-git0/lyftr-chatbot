.PHONY: up down logs test build clean

up:          ## build and start backend (:8000) and frontend (:3000)
	@test -f .env || cp .env.example .env
	docker compose up -d --build
	@echo "Frontend: http://localhost:3000   Backend: http://localhost:8000/health"

down:        ## stop the stack (SQLite volume is kept)
	docker compose down

logs:
	docker compose logs -f

build:
	docker compose build

test:        ## run backend tests in a throwaway container
	docker compose run --rm --no-deps -v "$(CURDIR)/backend:/src" -w /src backend sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"

clean:       ## stop and delete the SQLite volume
	docker compose down -v
