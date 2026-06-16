from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RouteReplacement:
    method: str
    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class RouterExtensionPlan:
    additive_route_names: tuple[str, ...] = ()
    replacements: tuple[RouteReplacement, ...] = ()

    def replaces(self, method: str, path: str) -> bool:
        normalised_method = method.upper()
        return any(
            replacement.method.upper() == normalised_method and replacement.path == path
            for replacement in self.replacements
        )
