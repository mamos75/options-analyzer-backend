#!/usr/bin/env python3
"""
Phase 0 Mock Server for Options Trading Dashboard
Pure stdlib implementation - no external dependencies required
Port: 8765
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone


def ts_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def fixture_market_decision():
    return {
        'timestamp': ts_now(),
        'spot': 105230.50,
        'decision': 'BULLISH',
        'confidence': 0.74,
        'signal_strength': 0.68,
        'regime': 'TRENDING_UP',
        'primary_signal': 'GEX_POSITIVE',
        'supporting_signals': ['MOPI_LONG', 'DEX_POSITIVE', 'VOL_CRUSH'],
        'opposing_signals': ['FUNDING_HIGH'],
        'action': 'HOLD_LONG',
        'stop_loss': 103500.00,
        'take_profit': 108000.00,
        'risk_reward': 2.6,
        'notes': 'Strong GEX support at 105k; dealer hedging flow bullish'
    }


def fixture_options_walls():
    return {
        'timestamp': ts_now(),
        'spot': 105230.50,
        'walls': [
            {'strike': 100000, 'type': 'CALL', 'gex': 1850000000, 'oi': 45200, 'label': 'MAJOR_SUPPORT'},
            {'strike': 105000, 'type': 'CALL', 'gex': 3200000000, 'oi': 62800, 'label': 'GAMMA_WALL'},
            {'strike': 110000, 'type': 'CALL', 'gex': 2750000000, 'oi': 51300, 'label': 'RESISTANCE'},
            {'strike': 115000, 'type': 'CALL', 'gex': 1200000000, 'oi': 28900, 'label': 'FAR_RESISTANCE'},
            {'strike': 100000, 'type': 'PUT',  'gex': -980000000,  'oi': 38700, 'label': 'PUT_FLOOR'},
            {'strike': 95000,  'type': 'PUT',  'gex': -2100000000, 'oi': 44100, 'label': 'MAJOR_PUT_WALL'},
        ],
        'gamma_flip_level': 103750.00,
        'net_gex': 8420000000,
        'top_call_wall': 105000,
        'top_put_wall': 95000
    }


def fixture_probability_engine():
    return {
        'timestamp': ts_now(),
        'spot': 105230.50,
        'horizon': '24h',
        'scenarios': [
            {'label': 'BULL_BREAKOUT',  'probability': 0.32, 'target': 109500, 'catalyst': 'GEX_UNWIND'},
            {'label': 'SIDEWAYS_GRIND', 'probability': 0.41, 'target': 105800, 'catalyst': 'VOL_CRUSH'},
            {'label': 'BEAR_REJECTION', 'probability': 0.27, 'target': 101200, 'catalyst': 'FUNDING_RESET'},
        ],
        'expected_move_1sd': 2850.00,
        'expected_move_1sd_pct': 2.71,
        'skew': -0.08,
        'term_structure_slope': 'CONTANGO_MILD',
        'vol_regime': 'LOW_VOL'
    }


def fixture_dashboard(variant=None):
    base = {
        'timestamp': ts_now(),
        'spot': 105230.50,
        'gex_total': 8420000000,
        'gex_pct_change_24h': 3.2,
        'dex_total': 1250000000,
        'net_delta': 0.38,
        'mopi_squeeze_heuristic': 0.72,
        'mopi_score': 0.65,
        'funding_rate': 0.0082,
        'open_interest_calls': 2840000000,
        'open_interest_puts': 1960000000,
        'pc_ratio': 0.69,
        'vol_30d': 0.58,
        'vol_7d': 0.52,
        'iv_atm': 0.61,
        'regime': 'TRENDING_UP',
        'gamma_flip': 103750.00,
        'dealer_net_delta': 12300000,
        'weather_color': '#2196F3',
        'weather_state': 'BULLISH',
        'gex_regime': 'POSITIVE'
    }
    if variant == 'gex_zero':
        base['gex_total'] = 0
    elif variant == 'squeeze_zero':
        base['mopi_squeeze_heuristic'] = 0
    return base


def fixture_dealer_pressure():
    return {
        'timestamp': ts_now(),
        'spot': 105230.50,
        'net_delta_pressure': 12300000,
        'pressure_direction': 'LONG',
        'pressure_magnitude': 0.68,
        'flows': [
            {'time': '08:00', 'delta': 3200000,  'direction': 'BUY'},
            {'time': '10:00', 'delta': -1100000, 'direction': 'SELL'},
            {'time': '12:00', 'delta': 4800000,  'direction': 'BUY'},
            {'time': '14:00', 'delta': 2900000,  'direction': 'BUY'},
            {'time': '16:00', 'delta': 2500000,  'direction': 'BUY'},
        ],
        'charm_flow': -450000,
        'vanna_flow': 1800000,
        'regime_note': 'Dealers net long; supportive for spot'
    }


def fixture_narrative(variant=None):
    base = {
        'timestamp': ts_now(),
        'phrase_synthese': (
            'BTC optionality remains skewed bullish with positive GEX pinning near 105k. '
            'Dealer flow supports upside continuation. Vol crush expected into weekend.'
        ),
        'banner_message': 'Test banner',
        'sentiment': 'BULLISH',
        'key_themes': ['GEX_PIN', 'VOL_CRUSH', 'DEALER_SUPPORT'],
        'risk_factors': ['HIGH_FUNDING', 'MACRO_UNCERTAINTY'],
        'week_summary': (
            'Week opened with strong call buying at 105k strike. '
            'Net GEX remains positive indicating dealer short gamma hedge support. '
            'Watch for gamma unwind above 108k.'
        )
    }
    if variant == 'xss':
        # Raw unescaped HTML - escaping is the frontend job
        base['phrase_synthese'] = (
            '<b>test XSS</b> BTC optionality skewed bullish; '
            '<script>alert(1)</script> dealer flow positive.'
        )
    elif variant == 'no_banner':
        del base['banner_message']
    return base


def fixture_leaderboard():
    return {
        'timestamp': ts_now(),
        'period': '7d',
        'models': [
            {
                'rank': 1,
                'name': 'GEX_MOMENTUM_V3',
                'accuracy': 0.71,
                'sharpe': 1.84,
                'total_signals': 48,
                'correct_signals': 34,
                'pnl_pct': 8.3,
                'regime_fit': 'TRENDING'
            },
            {
                'rank': 2,
                'name': 'VANNA_CHARM_FLOW',
                'accuracy': 0.67,
                'sharpe': 1.52,
                'total_signals': 42,
                'correct_signals': 28,
                'pnl_pct': 5.7,
                'regime_fit': 'RANGING'
            },
            {
                'rank': 3,
                'name': 'SKEW_REVERSAL_M1',
                'accuracy': 0.63,
                'sharpe': 1.21,
                'total_signals': 55,
                'correct_signals': 35,
                'pnl_pct': 3.2,
                'regime_fit': 'VOLATILE'
            },
            {
                'rank': 4,
                'name': 'FUNDING_ARBIT_V2',
                'accuracy': 0.59,
                'sharpe': 0.94,
                'total_signals': 38,
                'correct_signals': 22,
                'pnl_pct': 1.8,
                'regime_fit': 'RANGING'
            }
        ]
    }


def fixture_vol_structure():
    return {
        'timestamp': ts_now(),
        'spot': 105230.50,
        'term_structure': [
            {'expiry': '1d',  'iv': 0.52, 'rv': 0.48, 'premium': 0.04},
            {'expiry': '7d',  'iv': 0.58, 'rv': 0.51, 'premium': 0.07},
            {'expiry': '14d', 'iv': 0.61, 'rv': 0.53, 'premium': 0.08},
            {'expiry': '30d', 'iv': 0.65, 'rv': 0.55, 'premium': 0.10},
            {'expiry': '60d', 'iv': 0.68, 'rv': 0.57, 'premium': 0.11},
            {'expiry': '90d', 'iv': 0.71, 'rv': 0.58, 'premium': 0.13},
        ],
        'skew_25d': -0.08,
        'skew_10d': -0.14,
        'rr_25d': 0.062,
        'fly_25d': 0.018,
        'vol_regime': 'CONTANGO_MILD',
        'atm_vol': 0.61,
        'vol_of_vol': 0.22
    }


def fixture_mopi_vs_btc(period='7d'):
    periods = {
        '7d':  {'n': 7,  'start_spot': 98200},
        '14d': {'n': 14, 'start_spot': 94500},
        '30d': {'n': 30, 'start_spot': 88000},
    }
    cfg = periods.get(period, periods['7d'])
    n = cfg['n']
    start = cfg['start_spot']
    step = (105230 - start) / n
    data = []
    for i in range(n):
        spot = round(start + step * i, 2)
        mopi = round(0.30 + 0.40 * (i / n) + ((-1) ** i) * 0.05, 4)
        data.append({
            'day': i + 1,
            'spot': spot,
            'mopi_score': mopi,
            'mopi_squeeze': round(mopi * 0.95, 4)
        })
    return {
        'timestamp': ts_now(),
        'period': period,
        'data': data,
        'correlation': 0.82,
        'mopi_lead_days': 1.5
    }


def fixture_vex_cex():
    return {
        'timestamp': ts_now(),
        'spot': 105230.50,
        'vex_total': 1850000000.0,
        'cex_total': 1280000000.0,
        'vex_total_fmt': '1.85B',
        'cex_total_fmt': '1.28B',
        'vex_direction': 'BULLISH',
        'cex_direction': 'BULLISH',
        'vex_interpretation': 'Strong vanna exposure supports upside',
        'cex_interpretation': 'Charm flow adds mild tailwind',
        'vex_by_strike': [{'strike': 105000, 'vex': 850000000}, {'strike': 110000, 'vex': 1000000000}],
        'cex_by_strike': [{'strike': 100000, 'cex': 480000000}, {'strike': 105000, 'cex': 800000000}],
        'gamma_flip': 103750.0,
        'gamma_flip_dist_pct': 1.4,
        'gamma_flip_side': 'BELOW',
        'gamma_flip_regime': 'NEAR_FLIP',
        'gamma_flip_interpretation': 'Spot near gamma flip — vol may expand',
        'vex_score': 0.63,
        'cex_score': 0.71,
        'vex_cex_ratio': 0.89,
        'vex_pressure': 'MODERATE_LONG',
        'cex_pressure': 'STRONG_LONG',
        'combined_signal': 'BULLISH',
        'breakdown': {
            'deribit_oi_calls': 1850000000,
            'deribit_oi_puts': 1280000000,
            'bybit_oi_calls': 640000000,
            'bybit_oi_puts': 420000000,
            'binance_vol_24h': 2100000000,
        }
    }

def fixture_vex_cex_history(period='7d'):
    n_map = {'7d': 7, '14d': 14, '30d': 30}
    n = n_map.get(period, 7)
    data = []
    for i in range(n):
        data.append({
            'day': i + 1,
            'vex_score': round(0.45 + 0.20 * (i / n), 4),
            'cex_score': round(0.50 + 0.22 * (i / n), 4),
            'spot': round(98000 + 7230 * (i / n), 2)
        })
    return {
        'timestamp': ts_now(),
        'period': period,
        'data': data
    }


def fixture_bme_status(variant=None):
    if variant == 'no_edge':
        return {
            'timestamp': ts_now(),
            'model': 'BME_V2',
            'dir_winrate': 0.53,
            'n_dir_attempted': 30,
            'n_dir_correct': 16,
            'eval_is_oos': True,
            'n_out_of_sample': 30,
            'n_overlap_excluded': 2,
            'has_edge': False,
            'wilson_lb': 0.37,
            'confidence_level': 0.95,
            'note': 'WR=53%/n=30 wilson_lower~0.37 < 0.50, no directional edge detected',
            'last_updated': ts_now()
        }
    return {
        'timestamp': ts_now(),
        'model': 'BME_V2',
        'dir_winrate': 0.70,
        'n_dir_attempted': 60,
        'n_dir_correct': 42,
        'eval_is_oos': True,
        'n_out_of_sample': 60,
        'n_overlap_excluded': 5,
        'has_edge': True,
        'wilson_lb': 0.58,
        'confidence_level': 0.95,
        'note': 'WR=70%/n=60 wilson_lower~0.58 > 0.50, edge confirmed',
        'last_updated': ts_now()
    }


def fixture_snapshot():
    return {
        'snapshot_ts': ts_now(),
        'spot': 105230.50,
        'dashboard': fixture_dashboard(),
        'walls': fixture_options_walls(),
        'dealer': fixture_dealer_pressure(),
        'squeeze': {
            'mopi_squeeze_heuristic': 0.72,
            'squeeze_score': 0.68,
            'squeeze_direction': 'LONG',
            'threshold': 0.65
        },
        'narrative': fixture_narrative(),
        'gravity': {
            'gravity_level': 105000,
            'gravity_strength': 0.84,
            'pull_direction': 'UP',
            'distance_to_gravity': 230.50
        },
        'bme_status': fixture_bme_status()
    }


class MockHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        sys.stderr.write('[MOCK] %s %s\n' % (self.address_string(), fmt % args))

    def send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')

    def send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json({'error': message, 'status': status}, status=status)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        variant = qs.get('variant', [None])[0]
        period   = qs.get('period',  ['7d'])[0]

        if path == '/api/snapshot':
            if variant == 'down':
                self.send_error_json(500, 'Snapshot endpoint failure (simulated)')
                return
            self.send_json(fixture_snapshot())
            return

        routes = {
            '/api/market_decision':         fixture_market_decision,
            '/api/options_walls':           fixture_options_walls,
            '/api/probability_engine':      fixture_probability_engine,
            '/api/dashboard':               lambda: fixture_dashboard(variant),
            '/api/dealer_pressure':         fixture_dealer_pressure,
            '/api/narrative':               lambda: fixture_narrative(variant),
            '/api/model_arena/leaderboard': fixture_leaderboard,
            '/api/vol_structure':           fixture_vol_structure,
            '/api/mopi_vs_btc':             lambda: fixture_mopi_vs_btc(period),
            '/api/vex_cex':                 fixture_vex_cex,
            '/api/vex_cex_history':         lambda: fixture_vex_cex_history(period),
            '/api/model_arena/bme_status':  lambda: fixture_bme_status(variant),
        }

        if path in routes:
            try:
                self.send_json(routes[path]())
            except Exception as exc:
                self.send_error_json(500, str(exc))
            return

        self.send_error_json(404, 'Unknown endpoint: %s' % path)


def main():
    host = '0.0.0.0'
    port = 8765
    server = HTTPServer((host, port), MockHandler)
    sys.stderr.write('[MOCK] Phase-0 mock server listening on http://%s:%d\n' % (host, port))
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write('\n[MOCK] Server stopped.\n')


if __name__ == '__main__':
    main()
