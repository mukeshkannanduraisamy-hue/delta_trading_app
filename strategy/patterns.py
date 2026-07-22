def get_candle_details(candle: dict) -> tuple:
    """
    Helper function to extract candle characteristics.
    """
    open_p = candle['open']
    close_p = candle['close']
    high_p = candle['high']
    low_p = candle['low']
    body = abs(close_p - open_p)
    is_bullish = close_p > open_p
    is_bearish = close_p < open_p
    upper_shadow = high_p - max(open_p, close_p)
    lower_shadow = min(open_p, close_p) - low_p
    midpoint = (open_p + close_p) / 2
    return open_p, close_p, high_p, low_p, body, is_bullish, is_bearish, upper_shadow, lower_shadow, midpoint


def detect_patterns(candles: list[dict]) -> list[dict]:
    """
    Scan the given list of candles and return a list of detected pattern dicts.
    
    Each pattern dict contains:
    - name: The name of the pattern
    - type: 'bullish', 'bearish', or 'neutral'
    - strength: 0-100 score of the pattern's strength
    - index: The index of the candle where the pattern completed
    """
    patterns = []
    if len(candles) == 0:
        return patterns
        
    for i in range(len(candles)):
        c = candles[i]
        o, cl, h, l, b, is_bull, is_bear, us, ls, mid = get_candle_details(c)
        
        # --- Single Candle Patterns ---
        if h - l > 0 and b < 0.1 * (h - l):
            patterns.append({'name': 'Doji', 'type': 'neutral', 'strength': 50, 'index': i})
            
        if b > 0 and ls > 2 * b and us < 0.2 * b:
            patterns.append({'name': 'Hammer', 'type': 'bullish', 'strength': 70, 'index': i})
            
        if b > 0 and us > 2 * b and ls < 0.2 * b:
            patterns.append({'name': 'Shooting Star', 'type': 'bearish', 'strength': 70, 'index': i})
            
        if b > 0 and (us + ls) < 0.05 * b:
            if is_bull:
                patterns.append({'name': 'Marubozu', 'type': 'bullish', 'strength': 80, 'index': i})
            elif is_bear:
                patterns.append({'name': 'Marubozu', 'type': 'bearish', 'strength': 80, 'index': i})
                
        # --- Double Candle Patterns ---
        if i >= 1:
            pc = candles[i-1]
            po, pcl, ph, pl, pb, p_is_bull, p_is_bear, pus, pls, pmid = get_candle_details(pc)
            
            # Engulfing
            if p_is_bear and is_bull and cl > po and o < pcl:
                patterns.append({'name': 'Bullish Engulfing', 'type': 'bullish', 'strength': 80, 'index': i})
            if p_is_bull and is_bear and cl < po and o > pcl:
                patterns.append({'name': 'Bearish Engulfing', 'type': 'bearish', 'strength': 80, 'index': i})
                
            # Harami
            if p_is_bear and is_bull and o > pcl and cl < po:
                patterns.append({'name': 'Bullish Harami', 'type': 'bullish', 'strength': 60, 'index': i})
            if p_is_bull and is_bear and o < pcl and cl > po:
                patterns.append({'name': 'Bearish Harami', 'type': 'bearish', 'strength': 60, 'index': i})
                
            # Piercing Line
            if p_is_bear and is_bull and o < pl and cl > pmid:
                patterns.append({'name': 'Piercing Line', 'type': 'bullish', 'strength': 75, 'index': i})
                
            # Dark Cloud Cover
            if p_is_bull and is_bear and o > ph and cl < pmid:
                patterns.append({'name': 'Dark Cloud Cover', 'type': 'bearish', 'strength': 75, 'index': i})
                
        # --- Triple Candle Patterns ---
        if i >= 2:
            ppc = candles[i-2]
            ppo, ppcl, pph, ppl, ppb, pp_is_bull, pp_is_bear, ppus, ppls, ppmid = get_candle_details(ppc)
            
            # Morning Star
            if pp_is_bear and pb < ppb * 0.3 and is_bull and cl > ppmid:
                patterns.append({'name': 'Morning Star', 'type': 'bullish', 'strength': 85, 'index': i})
                
            # Evening Star
            if pp_is_bull and pb < ppb * 0.3 and is_bear and cl < ppmid:
                patterns.append({'name': 'Evening Star', 'type': 'bearish', 'strength': 85, 'index': i})
                
            # Three White Soldiers
            if pp_is_bull and p_is_bull and is_bull:
                if pcl > ppcl and cl > pcl:
                    patterns.append({'name': 'Three White Soldiers', 'type': 'bullish', 'strength': 90, 'index': i})
                    
            # Three Black Crows
            if pp_is_bear and p_is_bear and is_bear:
                if pcl < ppcl and cl < pcl:
                    patterns.append({'name': 'Three Black Crows', 'type': 'bearish', 'strength': 90, 'index': i})

    return patterns


def pattern_score(candles: list[dict], signal_direction: str) -> int:
    """
    Calculates a 0-100 score based on recent patterns and desired signal direction.
    
    For BUY signals, bullish patterns increase the score towards 100.
    For SELL signals, bearish patterns increase the score towards 100.
    No pattern evaluates to 50 (neutral).
    """
    if not candles:
        return 50
        
    patterns = detect_patterns(candles)
    if not patterns:
        return 50
        
    score = 50
    signal_direction = signal_direction.upper()
    
    # Analyze recent patterns (patterns forming in the last 3 candles)
    recent_patterns = [p for p in patterns if p['index'] >= len(candles) - 3]
    
    for p in recent_patterns:
        # Scale strength: strength of 100 maps to +50/-50 from neutral base (50)
        score_mod = int(p['strength'] / 2)
        
        if signal_direction == 'BUY':
            if p['type'] == 'bullish':
                score = max(score, 50 + score_mod)
            elif p['type'] == 'bearish':
                score = min(score, 50 - score_mod)
                
        elif signal_direction == 'SELL':
            if p['type'] == 'bearish':
                score = max(score, 50 + score_mod)
            elif p['type'] == 'bullish':
                score = min(score, 50 - score_mod)
                
    return score
