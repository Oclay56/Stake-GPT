from __future__ import annotations

from app.local_ai_chat import build_ai_chat_console_command


def test_ai_chat_console_command_launches_ollama_chat(monkeypatch, tmp_path):
    monkeypatch.setenv("STAKE_GPT_LOCAL_AI_MODEL", "qwen3:8b")

    command = build_ai_chat_console_command(tmp_path)
    script = command[-1]

    assert command[:6] == [
        "powershell.exe",
        "-NoLogo",
        "-NoExit",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
    ]
    assert "Stake-GPT AI Chat" in script
    assert "qwen3:8b" in script
    assert " run 'qwen3:8b'" in script
