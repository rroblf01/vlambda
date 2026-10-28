"""Run this after `vcraft develop` to see the extension in action."""

from __future__ import annotations

import vlambda as m


def main() -> None:
	print(m.greet("world"))
	print(m.add(2, 3))
	counter = m.Counter()
	print(counter.increment())
	try:
		m.parse_int("not a number")
	except ValueError as exc:
		print(f"parse_int: {exc}")


if __name__ == "__main__":
	main()
