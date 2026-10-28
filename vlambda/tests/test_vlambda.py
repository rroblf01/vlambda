"""Tests for the scaffolded extension."""

import unittest

import vlambda as m


class TestScaffold(unittest.TestCase):
	def test_greet(self) -> None:
		self.assertEqual(m.greet("world"), "Hello, world!")

	def test_add(self) -> None:
		self.assertEqual(m.add(2, 3), 5)

	def test_counter(self) -> None:
		counter = m.Counter()
		self.assertEqual(counter.increment(), 1)

	def test_parse_int_rejects(self) -> None:
		with self.assertRaises(ValueError):
			m.parse_int("not a number")


if __name__ == "__main__":
	unittest.main()
