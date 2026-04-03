"""HTML login and register pages served by the Starlette app."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

_BASE_STYLE = """
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        background: #0f1117;
        color: #e1e4e8;
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
    }
    .card {
        background: #1c1f26;
        border: 1px solid #2d3139;
        border-radius: 12px;
        padding: 2rem;
        width: 100%;
        max-width: 400px;
        box-shadow: 0 4px 24px rgba(0, 0, 0, 0.3);
    }
    h1 {
        font-size: 1.5rem;
        margin-bottom: 0.25rem;
        color: #f0f0f0;
    }
    .subtitle {
        color: #8b949e;
        font-size: 0.875rem;
        margin-bottom: 1.5rem;
    }
    label {
        display: block;
        font-size: 0.875rem;
        color: #c9d1d9;
        margin-bottom: 0.375rem;
    }
    input[type="email"], input[type="password"] {
        width: 100%;
        padding: 0.625rem 0.75rem;
        background: #0d1117;
        border: 1px solid #30363d;
        border-radius: 6px;
        color: #e1e4e8;
        font-size: 0.875rem;
        margin-bottom: 1rem;
        outline: none;
        transition: border-color 0.15s;
    }
    input:focus {
        border-color: #58a6ff;
    }
    button {
        width: 100%;
        padding: 0.625rem;
        background: #238636;
        color: #fff;
        border: none;
        border-radius: 6px;
        font-size: 0.875rem;
        font-weight: 600;
        cursor: pointer;
        transition: background 0.15s;
    }
    button:hover { background: #2ea043; }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    .error {
        background: #3d1f1f;
        border: 1px solid #6e3630;
        color: #f88;
        padding: 0.5rem 0.75rem;
        border-radius: 6px;
        font-size: 0.8125rem;
        margin-bottom: 1rem;
        display: none;
    }
    .success {
        background: #1f3d1f;
        border: 1px solid #2ea043;
        color: #7ee787;
        padding: 0.5rem 0.75rem;
        border-radius: 6px;
        font-size: 0.8125rem;
        margin-bottom: 1rem;
        display: none;
    }
    .link {
        text-align: center;
        margin-top: 1rem;
        font-size: 0.8125rem;
        color: #8b949e;
    }
    .link a {
        color: #58a6ff;
        text-decoration: none;
    }
    .link a:hover { text-decoration: underline; }
"""


def _login_html() -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Login — KrewCLI</title>
    <style>{_BASE_STYLE}</style>
</head>
<body>
    <div class="card">
        <h1>KrewCLI</h1>
        <p class="subtitle">Sign in to your account</p>
        <div id="error" class="error"></div>
        <div id="success" class="success"></div>
        <form id="login-form">
            <label for="email">Email</label>
            <input type="email" id="email" name="email" required autocomplete="email" autofocus>
            <label for="password">Password</label>
            <input type="password" id="password" name="password" required autocomplete="current-password">
            <button type="submit" id="submit-btn">Sign in</button>
        </form>
        <p class="link">No account? <a href="/register">Register</a></p>
    </div>
    <script>
        const form = document.getElementById('login-form');
        const errorEl = document.getElementById('error');
        const successEl = document.getElementById('success');
        const submitBtn = document.getElementById('submit-btn');

        form.addEventListener('submit', async (e) => {{
            e.preventDefault();
            errorEl.style.display = 'none';
            successEl.style.display = 'none';
            submitBtn.disabled = true;
            submitBtn.textContent = 'Signing in\u2026';

            try {{
                const resp = await fetch('/auth/login', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        email: document.getElementById('email').value,
                        password: document.getElementById('password').value,
                    }}),
                }});
                const data = await resp.json();
                if (!resp.ok) {{
                    throw new Error(data.error || 'Login failed');
                }}
                localStorage.setItem('krewcli_token', data.access_token);
                successEl.textContent = 'Logged in successfully.';
                successEl.style.display = 'block';
                setTimeout(() => {{ window.location.href = '/'; }}, 600);
            }} catch (err) {{
                errorEl.textContent = err.message;
                errorEl.style.display = 'block';
            }} finally {{
                submitBtn.disabled = false;
                submitBtn.textContent = 'Sign in';
            }}
        }});
    </script>
</body>
</html>"""


def _register_html() -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Register — KrewCLI</title>
    <style>{_BASE_STYLE}</style>
</head>
<body>
    <div class="card">
        <h1>KrewCLI</h1>
        <p class="subtitle">Create a new account</p>
        <div id="error" class="error"></div>
        <div id="success" class="success"></div>
        <form id="register-form">
            <label for="email">Email</label>
            <input type="email" id="email" name="email" required autocomplete="email" autofocus>
            <label for="password">Password</label>
            <input type="password" id="password" name="password" required autocomplete="new-password"
                   minlength="8" maxlength="1024">
            <label for="confirm">Confirm password</label>
            <input type="password" id="confirm" name="confirm" required autocomplete="new-password"
                   minlength="8" maxlength="1024">
            <button type="submit" id="submit-btn">Create account</button>
        </form>
        <p class="link">Already have an account? <a href="/login">Sign in</a></p>
    </div>
    <script>
        const form = document.getElementById('register-form');
        const errorEl = document.getElementById('error');
        const successEl = document.getElementById('success');
        const submitBtn = document.getElementById('submit-btn');

        form.addEventListener('submit', async (e) => {{
            e.preventDefault();
            errorEl.style.display = 'none';
            successEl.style.display = 'none';

            const password = document.getElementById('password').value;
            const confirm = document.getElementById('confirm').value;
            if (password !== confirm) {{
                errorEl.textContent = 'Passwords do not match';
                errorEl.style.display = 'block';
                return;
            }}

            submitBtn.disabled = true;
            submitBtn.textContent = 'Creating account\u2026';

            try {{
                const resp = await fetch('/auth/register', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        email: document.getElementById('email').value,
                        password: password,
                    }}),
                }});
                const data = await resp.json();
                if (!resp.ok) {{
                    const msg = data.details
                        ? data.details.map(d => d.message).join('; ')
                        : (data.error || 'Registration failed');
                    throw new Error(msg);
                }}
                successEl.textContent = 'Account created! Redirecting to login\u2026';
                successEl.style.display = 'block';
                setTimeout(() => {{ window.location.href = '/login'; }}, 1000);
            }} catch (err) {{
                errorEl.textContent = err.message;
                errorEl.style.display = 'block';
            }} finally {{
                submitBtn.disabled = false;
                submitBtn.textContent = 'Create account';
            }}
        }});
    </script>
</body>
</html>"""


async def handle_login_page(request: Request) -> HTMLResponse:
    """GET /login — serve the login form."""
    return HTMLResponse(_login_html())


async def handle_register_page(request: Request) -> HTMLResponse:
    """GET /register — serve the registration form."""
    return HTMLResponse(_register_html())


page_routes = [
    Route("/login", handle_login_page, methods=["GET"]),
    Route("/register", handle_register_page, methods=["GET"]),
]
