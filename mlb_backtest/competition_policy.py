"""The two fixed, immutable MLB competition populations."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CompetitionPolicy:
    competition_scope: str
    research_policy_version: str | None
    target_game_types: tuple[str, ...]
    recent_game_types: tuple[str, ...]


REGULAR_SEASON_V1 = CompetitionPolicy('regular-season', None, ('R',), ('R',))
POSTSEASON_ML_RESEARCH_V1 = CompetitionPolicy('postseason-research-v1', 'POSTSEASON_ML_RESEARCH_V1', ('F','D','L','W'), ('R','F','D','L','W'))


def resolve_competition_policy(scope: str) -> CompetitionPolicy:
    for policy in (REGULAR_SEASON_V1, POSTSEASON_ML_RESEARCH_V1):
        if scope == policy.competition_scope:
            return policy
    raise ValueError('unsupported competition scope')


def validate_competition_policy(policy: CompetitionPolicy) -> CompetitionPolicy:
    if not isinstance(policy, CompetitionPolicy) or policy not in (REGULAR_SEASON_V1, POSTSEASON_ML_RESEARCH_V1):
        raise ValueError('unsupported competition policy')
    return resolve_competition_policy(policy.competition_scope)


def history_population(policy: CompetitionPolicy) -> dict[str, object]:
    policy = validate_competition_policy(policy)
    return {'recent_game_types': list(policy.recent_game_types), 'season': 'TARGET_SEASON', 'cutoff': 'D_MINUS_1_AMERICA_NEW_YORK', 'window_definitions': 'UNCHANGED_FROZEN_V5_1', 'career_bf_population': 'REGULAR_SEASON', 'league_era_population': 'REGULAR_SEASON'}


def policy_identity(policy: CompetitionPolicy) -> dict[str, object]:
    policy = validate_competition_policy(policy)
    if policy == REGULAR_SEASON_V1:
        return {}
    return {'competition_scope': policy.competition_scope, 'research_policy_version': policy.research_policy_version, 'history_population': history_population(policy)}


def research_source(source: str, policy: CompetitionPolicy) -> str:
    return source if validate_competition_policy(policy) == REGULAR_SEASON_V1 else source + '_postseason_research_v1'
