# Contributing to Procurement Bot

Thank you for your interest in contributing! This document outlines the process and guidelines for contributing to this project.

---

## 📋 Table of Contents

- [Code of Conduct](#-code-of-conduct)
- [How Can I Contribute?](#-how-can-i-contribute)
- [Development Setup](#-development-setup)
- [Coding Standards](#-coding-standards)
- [Commit Message Convention](#-commit-message-convention)
- [Pull Request Process](#-pull-request-process)

---

## 🤝 Code of Conduct

By participating in this project, you agree to maintain a respectful and inclusive environment. Be kind, be patient, and assume good intentions.

---

## 💡 How Can I Contribute?

### Reporting Bugs

Before creating a bug report, please check existing issues to avoid duplicates.

When reporting a bug, include:
- A clear and descriptive title.
- Steps to reproduce the issue.
- Expected vs. actual behavior.
- Your environment (OS, Python version, PostgreSQL version).
- Relevant logs (with secrets redacted).

### Suggesting Features

Feature suggestions are welcome. Please open an issue describing:
- The problem you're trying to solve.
- Your proposed solution.
- Any alternatives you've considered.

### Submitting Changes

1. Fork the repository.
2. Create a feature branch (`git checkout -b feature/your-feature-name`).
3. Make your changes with clear commits.
4. Push to your fork and open a Pull Request.

---

## 🛠️ Development Setup

### 1. Fork and clone

```bash
git clone https://github.com/<your-username>/procurement-bot.git
cd procurement-bot
```

### 2. Create a virtual environment

```bash
python -m venv .venv

# Linux/macOS
source .venv/bin/activate

# Windows
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your local values. **Never commit this file.**

### 5. Set up the database

```bash
createdb procurement_bot
alembic upgrade head
```

### 6. Run the bot

```bash
python main.py
```

---

## 📐 Coding Standards

### Python Style

- Follow **PEP 8**.
- Use **type hints** for all function signatures.
- Maximum line length: **100 characters**.
- Use **async/await** for all I/O operations.

### Project Conventions

- **Separation of concerns:** Business logic belongs in `app/services/`, not in handlers.
- **Multi-tenancy:** Every database query must be scoped to a `company_id`.
- **No hardcoded secrets:** Always read from `settings`.
- **Logging:** Use the project's logger, not `print()`.
- **Validation:** Use Pydantic models for all inputs.

### Example Structure

```python
# app/services/example_service.py
from app.database.models import Example
from app.core.logger import logger


async def create_example(session, company_id: int, name: str) -> Example:
    """Create a new example scoped to a company."""
    logger.info(f"Creating example: {name} for company {company_id}")
    example = Example(company_id=company_id, name=name)
    session.add(example)
    await session.flush()
    return example
```

---

## 📝 Commit Message Convention

We follow the **Conventional Commits** specification:

```
<type>(<scope>): <subject>
```

### Types

| Type | Meaning |
|------|---------|
| `feat` | A new feature |
| `fix` | A bug fix |
| `docs` | Documentation changes |
| `style` | Code style changes (formatting, no logic change) |
| `refactor` | Code refactoring (no feature change) |
| `test` | Adding or updating tests |
| `chore` | Build, dependencies, or tooling changes |

### Examples

```
feat(rfq): add directed RFQ creation flow
fix(pricing): handle missing custom price fallback correctly
docs(readme): update installation instructions
refactor(services): extract common query logic
test(company): add tests for company approval
```

---

## 🔄 Pull Request Process

1. **Update your fork** with the latest `main` branch before starting.
2. **Create a focused branch** — one feature or fix per PR.
3. **Write clear commits** following the convention above.
4. **Test your changes** locally — the bot must start and run.
5. **Update documentation** if your change affects usage.
6. **Open a Pull Request** with:
   - A clear title.
   - A description of what changed and why.
   - Reference to any related issues.
7. **Be responsive** to review comments.

### PR Checklist

- [ ] Code follows project conventions.
- [ ] All database queries are scoped to a `company_id`.
- [ ] No secrets or credentials are committed.
- [ ] Changes tested locally.
- [ ] Documentation updated if needed.

---

## 🔒 Security

If you discover a security vulnerability, **do not open a public issue**. Instead, contact the maintainer directly:

- Email: ghashadi092@gmail.com

All security reports will be handled confidentially.

---

## 📬 Questions?

If you have any questions, feel free to open an issue or reach out:

- GitHub: [@ghassonshadi5-tech](https://github.com/ghassonshadi5-tech)
- Email: ghashadi092@gmail.com

---

<p align="center"><sub>Thank you for contributing! 🙌</sub></p>