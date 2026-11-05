"""Single-game results and lobby summaries; historical series keep their meaning."""


def match_info(game):
    if game.get('match_format') == 'single':
        status = game.get('match_status', 'active')
        winner = (game['state'].get('result') or {}).get('winner') if status == 'completed' else None
        return {'format': 'single', 'status': status, 'winner': winner,
                'max_racks': 1, 'deciding': False, 'match_point': False}
    series = game.get('match_format') == 'best_of_3'
    status = game.get('match_status', 'active') if series else ('legacy' if game['status'] == 'finished' else 'active')
    winner = game['score'].index(2) if series and status == 'completed' and 2 in game['score'] else None
    return {'format': 'best_of_3' if series else 'legacy', 'status': status,
            'winner': winner, 'max_racks': 3 if series else None,
            'deciding': series and game['score'] == [1, 1],
            'match_point': series and status == 'active' and 1 in game['score']}


def is_active(game):
    return match_info(game)['status'] == 'active'


def has_history(game):
    """Finished racks remain readable while their series awaits another invitation."""
    return not is_active(game) or bool(game.get('result_reports'))


def reserves_players(game):
    """Saved progress occupies players only while the table is running."""
    return is_active(game) and game['status'] == 'running'


def summary(game):
    return {**{key: game[key] for key in ('id', 'players', 'status', 'rack', 'score', 'updated_at', 'error')},
            'match': match_info(game)}


def standings(games):
    stats = {actor: {'actor': actor, 'wins': 0, 'losses': 0, 'match_wins': 0, 'match_losses': 0}
             for actor in ('user', 'aion', 'connor')}
    for game in games:
        for seat, actor in enumerate(game['players']):
            stats[actor]['wins'] += game['score'][seat]
            stats[actor]['losses'] += game['score'][1-seat]
        info = match_info(game)
        if info['winner'] is not None:
            for seat, actor in enumerate(game['players']):
                stats[actor]['match_wins' if seat == info['winner'] else 'match_losses'] += 1
    most_wins = max(s['wins'] for s in stats.values())
    for stat in stats.values():
        played = stat['wins'] + stat['losses']
        stat['win_rate'] = round(stat['wins'] * 100 / played) if played else None
        stat['leader'] = most_wins > 0 and stat['wins'] == most_wins
    return list(stats.values())
