"""Explicit registry with stable iteration order and no execution at import."""
class AlgorithmRegistry:
    def __init__(self):
        self._strategies = {}

    def register(self, strategy):
        key = strategy.metadata.key
        if key in self._strategies:
            raise ValueError(f'Algorithm already registered: {key}')
        self._strategies[key] = strategy

    def get(self, key):
        try:
            return self._strategies[key]
        except KeyError:
            raise ValueError(f'Unknown algorithm: {key}') from None

    def keys(self):
        return tuple(sorted(self._strategies))
