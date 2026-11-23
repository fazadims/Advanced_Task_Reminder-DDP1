"""Shared fast TestCase base class for tasks test suite."""

from django.test import TestCase, override_settings


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class FastTestCase(TestCase):
    """Uses Django's built-in fast MD5PasswordHasher for rapid test user creation."""
