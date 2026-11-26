.DEFAULT_GOAL := help
.PHONY: docs test
SRC_DIRS = ./tutorobjectstorage ./tests

# Warning: These checks are run on every PR.
test: test-lint test-format test-types test-unit test-pythonpackage # Run some static checks.

test-format: ## Run code formatting tests.
	ruff format --check --diff $(SRC_DIRS)

test-lint: ## Run code linting tests
	ruff check ${SRC_DIRS}

test-types: ## Run type checks.
	mypy --exclude=templates --ignore-missing-imports --implicit-reexport --strict ./tutorobjectstorage

test-unit: ## Run unit tests: plugin loading and template rendering.
	python -m pytest -q tests

build-pythonpackage: ## Build the "tutor-objectstorage" python package for upload to pypi
	python -m build --sdist

test-pythonpackage: build-pythonpackage ## Test that package can be uploaded to pypi
	twine check dist/*.tar.gz

format: ## Format code automatically.
	ruff format ${SRC_DIRS}

fix-lint: ## Fix lint errors automatically
	ruff check --fix ${SRC_DIRS}

changelog-entry: ## Create a new changelog entry.
	scriv create

changelog: ## Collect changelog entries in the CHANGELOG.md file.
	scriv collect

version: ## Print the current tutor-objectstorage version
	@python -c 'import io, os; about = {}; exec(io.open(os.path.join("tutorobjectstorage", "__about__.py"), "rt", encoding="utf-8").read(), about); print(about["__version__"])'

ESCAPE =
help: ## Print this help.
	@grep -E '^([a-zA-Z_-]+:.*?## .*|######* .+)$$' Makefile \
		| sed 's/######* \(.*\)/@               $(ESCAPE)[1;31m\1$(ESCAPE)[0m/g' | tr '@' '\n' \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "\033[33m%-30s\033[0m %s\n", $$1, $$2}'
