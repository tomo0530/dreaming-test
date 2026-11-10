.PHONY: all-lint \
	lint \
	format \
	type-check \
	lint-fix \
	format-fix \
	fix \
	test \
	secret-scan \
	help

# Python Macros (all targets run on the host through uv)
PYTHON_ENV_RUNNER := uv run
PYTHON_TEST_RUNNER := $(PYTHON_ENV_RUNNER) pytest
PYTHON_LINT_RUNNER := $(PYTHON_ENV_RUNNER) ruff check --config ./ruff.toml
PYTHON_LINT_FIX_RUNNER := $(PYTHON_LINT_RUNNER) --fix
PYTHON_FORMAT_FIX_RUNNER := $(PYTHON_ENV_RUNNER) ruff format --config ./ruff.toml
PYTHON_FORMAT_RUNNER := $(PYTHON_FORMAT_FIX_RUNNER) --check
PYTHON_TYPE_CHECK_RUNNER := $(PYTHON_ENV_RUNNER) mypy --config-file ./mypy.ini

# Security Macros
GITLEAKS := gitleaks

# -------------------------------------------------------------------
# make targets
# -------------------------------------------------------------------

# Python Linting, Formatting, Type Checking targets

all-lint: ## Run format check, type-check, and lint in this order (Usage: make all-lint <file_or_directory>. Default is current directory)
	$(eval args := $(filter-out $@,$(MAKECMDGOALS)))
	$(MAKE) --no-print-directory format $(args)
	$(MAKE) --no-print-directory type-check $(args)
	$(MAKE) --no-print-directory lint $(args)

lint: ## Run linters in check mode (Usage: make lint <file_or_directory>. Default is current directory). To auto-fix, use 'make lint-fix'
	$(eval args := $(filter-out $@,$(MAKECMDGOALS)))
	$(PYTHON_LINT_RUNNER) $(args)

format: ## Run formatters in check mode (Usage: make format <file_or_directory>. Default is current directory). To auto-fix, use 'make format-fix'
	$(eval args := $(filter-out $@,$(MAKECMDGOALS)))
	$(PYTHON_FORMAT_RUNNER) $(args)

type-check: ## Run type checking (Usage: make type-check <file_or_directory>. Default is current directory)
	$(eval args := $(filter-out $@,$(MAKECMDGOALS)))
	$(eval args := $(if $(strip $(args)),$(args),.))
	$(PYTHON_TYPE_CHECK_RUNNER) $(args)

lint-fix: ## Run linters in fix mode (Usage: make lint-fix <file_or_directory>. Default is current directory)
	$(eval args := $(filter-out $@,$(MAKECMDGOALS)))
	$(PYTHON_LINT_FIX_RUNNER) $(args)

format-fix: ## Run formatters in fix mode (Usage: make format-fix <file_or_directory>. Default is current directory)
	$(eval args := $(filter-out $@,$(MAKECMDGOALS)))
	$(PYTHON_FORMAT_FIX_RUNNER) $(args)

fix: ## Run linters and formatters in fix mode (Usage: make fix <file_or_directory>. Default is current directory)
	$(MAKE) --no-print-directory lint-fix $(filter-out $@,$(MAKECMDGOALS))
	$(MAKE) --no-print-directory format-fix $(filter-out $@,$(MAKECMDGOALS))

test: ## Run tests (Usage: make test <test_path> or make test <directory_with_tests>)
	$(PYTHON_TEST_RUNNER) $(filter-out $@,$(MAKECMDGOALS))

# Security targets
secret-scan: ## Scan working tree paths for secrets (Usage: make secret-scan <path>. Default path is .)
	@if ! command -v $(GITLEAKS) >/dev/null 2>&1; then \
		echo "Error: 'gitleaks' is not installed." >&2; \
		echo "  Please install it first:" >&2; \
		echo "    macOS:   brew install gitleaks" >&2; \
		echo "    Linux:   See https://github.com/gitleaks/gitleaks#installing" >&2; \
		exit 1; \
	fi
	$(eval args := $(filter-out $@,$(MAKECMDGOALS)))
	$(eval args := $(if $(strip $(args)),$(args),.))
	$(GITLEAKS) dir --redact --verbose $(args)

help: ## Show help (auto-generated)
	@awk 'BEGIN{FS=":.*## "}; /^[a-zA-Z0-9_-]+:.*## /{printf "  %-28s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# Absorb positional arguments such as `make test <path>` as no-op goals.
%:
	@:
