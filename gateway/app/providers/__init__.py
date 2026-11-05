"""Registro dos providers. Provider novo: criar a classe e incluir aqui.

A ordem desta lista só vale na PRIMEIRA vez que o provider aparece (vai pro fim
da cadeia, desligado se `default_enabled = False`). Depois, ordem e liga/desliga
são do painel.
"""
from .edge import EdgeProvider
from .piper import PiperProvider


def build_providers():
    providers = [EdgeProvider(), PiperProvider()]
    return {p.name: p for p in providers}
