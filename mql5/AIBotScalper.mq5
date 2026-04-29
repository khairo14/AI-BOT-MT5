//+------------------------------------------------------------------+
//|  AIBotScalper.mq5                                                |
//|  EVOTRADE-AI — Expert Advisor for scalping order execution       |
//|                                                                  |
//|  Role: receive scalping signals from the Python backend via the  |
//|  Common/Files/evotrade/ shared directory, execute orders natively|
//|  inside MT5 (1–5 ms latency vs 50–300 ms from Python), manage   |
//|  trailing stops + breakeven on all open scalp positions, and     |
//|  write fill results back for the Python backend to consume.      |
//|                                                                  |
//|  Communication protocol (file-based, no TCP/pipe required):      |
//|    Python writes:  Common/Files/evotrade/ea_cmd_<id>.json        |
//|    EA writes:      Common/Files/evotrade/ea_res_<id>.json        |
//|    EA updates:     Common/Files/evotrade/ea_heartbeat.txt        |
//|                                                                  |
//|  Installation:                                                   |
//|    1. Copy AIBotScalper.mq5 to MT5 terminal MQL5/Experts/        |
//|    2. Compile in MetaEditor (F7)                                 |
//|    3. Attach to ANY chart (e.g. EURUSD M1) — symbol on the      |
//|       chart does not matter; EA manages all scalp positions.     |
//|    4. Enable "Allow algo trading" in MT5 Tools → Options         |
//|    5. Set InpMagicNumber to 20260318 (must match Python)         |
//+------------------------------------------------------------------+
#property copyright "EVOTRADE-AI"
#property version   "1.00"
#property strict
#property description "EVOTRADE-AI scalping execution EA"

//+------------------------------------------------------------------+
//|  Input Parameters                                                |
//+------------------------------------------------------------------+
input int    InpMagicNumber         = 20260318; // Magic number (must match Python BOT_MAGIC)
input double InpTrailingStopPips    = 8.0;      // Default trailing stop distance in pips (overridden per-ticket by Python)
input double InpBreakevenPips       = 5.0;      // Default breakeven pips (overridden per-ticket by Python; 0=disabled)
input int    InpMaxCommandAgeSecs   = 10;       // Discard commands older than N seconds (stale guard)
input int    InpDeviationPoints     = 30;       // Max slippage in points (raised from 20 to cover exotics/commodities)
input bool   InpTrailOnlyScalps     = true;     // Only trail positions tagged "scalp|" in comment

//+------------------------------------------------------------------+
//|  File path constants (relative to Common/Files/)                 |
//+------------------------------------------------------------------+
#define EVOTRADE_DIR    "evotrade\\"
#define HEARTBEAT_FILE  "evotrade\\ea_heartbeat.txt"
#define CMD_PATTERN     "evotrade\\ea_cmd_"
#define RES_PREFIX      "evotrade\\ea_res_"
//+------------------------------------------------------------------+
//  Per-ticket trail/breakeven overrides                             |
//  Python sends trail_pips and be_pips in the command JSON so that  |
//  ATR-based stops are respected on each individual trade.          |
//  Stored as ulong→double maps indexed by ticket number.            |
//+------------------------------------------------------------------+
ulong g_trail_tickets[];   // ticket numbers
double g_trail_pips[];     // trail distance in pips for that ticket
ulong g_be_tickets[];      // ticket numbers
double g_be_pips[];        // breakeven distance in pips for that ticket

// Store a per-ticket value in a parallel array pair
void _StoreTicketVal(ulong &keys[], double &vals[], ulong ticket, double val)
{
    int n = ArraySize(keys);
    for(int i = 0; i < n; i++)
    {
        if(keys[i] == ticket) { vals[i] = val; return; }
    }
    ArrayResize(keys, n + 1);
    ArrayResize(vals, n + 1);
    keys[n] = ticket;
    vals[n] = val;
}

// Lookup a per-ticket value; returns default_val if not found
double _GetTicketVal(ulong &keys[], double &vals[], ulong ticket, double default_val)
{
    int n = ArraySize(keys);
    for(int i = 0; i < n; i++)
        if(keys[i] == ticket) return vals[i];
    return default_val;
}

// Remove a closed ticket from both maps
void _RemoveTicket(ulong ticket)
{
    int n = ArraySize(g_trail_tickets);
    for(int i = 0; i < n; i++)
    {
        if(g_trail_tickets[i] == ticket)
        {
            for(int j = i; j < n - 1; j++)
            {
                g_trail_tickets[j] = g_trail_tickets[j+1];
                g_trail_pips[j]    = g_trail_pips[j+1];
            }
            ArrayResize(g_trail_tickets, n - 1);
            ArrayResize(g_trail_pips,    n - 1);
            break;
        }
    }
    n = ArraySize(g_be_tickets);
    for(int i = 0; i < n; i++)
    {
        if(g_be_tickets[i] == ticket)
        {
            for(int j = i; j < n - 1; j++)
            {
                g_be_tickets[j] = g_be_tickets[j+1];
                g_be_pips[j]    = g_be_pips[j+1];
            }
            ArrayResize(g_be_tickets, n - 1);
            ArrayResize(g_be_pips,    n - 1);
            break;
        }
    }
}
//+------------------------------------------------------------------+
//|  OnInit                                                          |
//+------------------------------------------------------------------+
int OnInit()
{
    // Create the evotrade folder if needed (FileOpen will create it implicitly)
    // Start 1-second timer for command polling + trailing stop management
    if(!EventSetTimer(1))
    {
        Alert("AIBotScalper: EventSetTimer failed — EA will not function correctly.");
        return INIT_FAILED;
    }

    // Write initial heartbeat so Python immediately sees the EA is live
    _WriteHeartbeat();

    Print("AIBotScalper initialised | Magic=", InpMagicNumber,
          " | TrailPips=", InpTrailingStopPips,
          " | BEPips=", InpBreakevenPips,
          " | MaxCmdAge=", InpMaxCommandAgeSecs, "s");
    return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//|  OnDeinit                                                        |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    EventKillTimer();

    // Remove heartbeat so Python knows EA is no longer running
    if(FileIsExist(HEARTBEAT_FILE, FILE_COMMON))
        FileDelete(HEARTBEAT_FILE, FILE_COMMON);

    Print("AIBotScalper stopped | reason=", reason);
}

//+------------------------------------------------------------------+
//|  OnTimer — fires every 1 second                                  |
//+------------------------------------------------------------------+
void OnTimer()
{
    _WriteHeartbeat();
    _ProcessCommands();
    _ManageTrailingStops();
}

//+------------------------------------------------------------------+
//|  OnTick — fires on every price tick for faster command pickup    |
//|  (belt + suspenders: Timer handles trailing; Tick handles orders)|
//+------------------------------------------------------------------+
void OnTick()
{
    _ProcessCommands();
}

//==========================================================================
//  HEARTBEAT
//==========================================================================

void _WriteHeartbeat()
{
    int fh = FileOpen(HEARTBEAT_FILE, FILE_WRITE | FILE_TXT | FILE_COMMON);
    if(fh == INVALID_HANDLE) return;
    // Write Unix timestamp so Python can check freshness
    FileWriteString(fh, IntegerToString((long)TimeCurrent()));
    FileClose(fh);
}

//==========================================================================
//  COMMAND PROCESSING
//==========================================================================

void _ProcessCommands()
{
    string found_name = "";
    long   search_handle = FileFindFirst(CMD_PATTERN + "*.json", found_name, FILE_COMMON);
    if(search_handle == INVALID_HANDLE)
        return;  // no pending commands

    do
    {
        // found_name is the relative path from Common/Files/ root
        // e.g. "evotrade\ea_cmd_abc123.json"
        _ProcessOneCommand(found_name);
    }
    while(FileFindNext(search_handle, found_name));

    FileFindClose(search_handle);
}

void _ProcessOneCommand(const string rel_path)
{
    // Read file content
    int fh = FileOpen(rel_path, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
    if(fh == INVALID_HANDLE)
    {
        Print("AIBotScalper: cannot open command file [", rel_path, "]");
        return;
    }
    string content = "";
    while(!FileIsEnding(fh))
        content += FileReadString(fh);
    FileClose(fh);

    if(StringLen(content) == 0)
    {
        FileDelete(rel_path, FILE_COMMON);
        return;
    }

    // Parse fields
    string sig_id   = _JsonStr(content, "id");
    string action   = _JsonStr(content, "action");
    string symbol   = _JsonStr(content, "symbol");
    string dir      = _JsonStr(content, "direction");
    double volume   = _JsonNum(content, "volume");
    double sl       = _JsonNum(content, "sl");
    double tp       = _JsonNum(content, "tp");
    string comment  = _JsonStr(content, "comment");
    long   created  = (long)_JsonNum(content, "created_ts");
    // Per-trade ATR-based trail/breakeven distances sent by Python.
    // 0.0 means "not provided" — falls back to the EA input defaults.
    double trail_pips_cmd = _JsonNum(content, "trail_pips");
    double be_pips_cmd    = _JsonNum(content, "be_pips");

    if(sig_id == "")
    {
        FileDelete(rel_path, FILE_COMMON);
        return;
    }

    // Stale guard: discard if command arrived too late
    long age = (long)TimeCurrent() - created;
    if(InpMaxCommandAgeSecs > 0 && age > InpMaxCommandAgeSecs)
    {
        Print("AIBotScalper: discarding stale command [", sig_id,
              "] age=", age, "s > limit=", InpMaxCommandAgeSecs, "s");
        FileDelete(rel_path, FILE_COMMON);
        _WriteResult(sig_id, 0, 0.0, "command_too_old");
        return;
    }

    // Execute
    if(action == "open")
    {
        _ExecuteOpen(sig_id, symbol, dir, volume, sl, tp, comment, trail_pips_cmd, be_pips_cmd);
    }
    else
    {
        Print("AIBotScalper: unknown action [", action, "] for signal [", sig_id, "]");
        _WriteResult(sig_id, 0, 0.0, "unknown_action_" + action);
    }

    // Always delete the command file once processed
    FileDelete(rel_path, FILE_COMMON);
}

void _ExecuteOpen(
    const string sig_id,
    const string symbol,
    const string dir,
    const double volume,
    const double sl,
    const double tp,
    const string comment,
    const double trail_pips_cmd,
    const double be_pips_cmd)
{
    // Get live tick
    MqlTick tick;
    if(!SymbolInfoTick(symbol, tick))
    {
        string err = "no_tick_data_for_" + symbol;
        Print("AIBotScalper: ", err);
        _WriteResult(sig_id, 0, 0.0, err);
        return;
    }

    ENUM_ORDER_TYPE order_type;
    double          price;

    if(dir == "BUY")
    {
        order_type = ORDER_TYPE_BUY;
        price      = tick.ask;
        if(sl >= price)
        {
            _WriteResult(sig_id, 0, 0.0, "buy_sl_above_ask");
            return;
        }
    }
    else if(dir == "SELL")
    {
        order_type = ORDER_TYPE_SELL;
        price      = tick.bid;
        if(sl <= price)
        {
            _WriteResult(sig_id, 0, 0.0, "sell_sl_below_bid");
            return;
        }
    }
    else
    {
        _WriteResult(sig_id, 0, 0.0, "invalid_direction_" + dir);
        return;
    }

    // Ensure symbol is available
    if(!SymbolSelect(symbol, true))
    {
        Print("AIBotScalper: SymbolSelect failed for ", symbol);
    }

    MqlTradeRequest req = {};
    MqlTradeResult  res = {};

    req.action       = TRADE_ACTION_DEAL;
    req.symbol       = symbol;
    req.volume       = volume;
    req.type         = order_type;
    req.price        = price;
    req.sl           = sl;
    req.tp           = (tp > 0.0) ? tp : 0.0;
    req.deviation    = InpDeviationPoints;
    req.magic        = (ulong)InpMagicNumber;
    req.comment      = StringSubstr(comment, 0, 31);   // MT5 limit: 31 chars
    req.type_time    = ORDER_TIME_GTC;
    req.type_filling = ORDER_FILLING_RETURN;            // XM requires RETURN, not IOC

    bool sent = OrderSend(req, res);

    if(!sent || (res.retcode != TRADE_RETCODE_DONE && res.retcode != 10009))
    {
        string err = "retcode=" + IntegerToString((int)res.retcode) + " " + res.comment;
        Print("AIBotScalper: OrderSend failed for [", sig_id, "]: ", err);
        _WriteResult(sig_id, 0, 0.0, err);
        return;
    }

    // Retrieve fill price from the opened position
    double fill_price = res.price;
    if(fill_price == 0.0 && PositionSelectByTicket(res.order))
        fill_price = PositionGetDouble(POSITION_PRICE_OPEN);

    Print("AIBotScalper: order placed | ticket=", res.order,
          " | ", symbol, " ", dir,
          " | vol=", volume,
          " | entry=", fill_price,
          " | sl=", sl,
          " | tp=", tp);

    // Store per-ticket trail/breakeven distances for _ManageTrailingStops.
    // Use the values from the command if provided; otherwise keep 0.0 so
    // _ManageTrailingStops falls back to the EA input defaults.
    if(trail_pips_cmd > 0.0)
        _StoreTicketVal(g_trail_tickets, g_trail_pips, res.order, trail_pips_cmd);
    if(be_pips_cmd > 0.0)
        _StoreTicketVal(g_be_tickets, g_be_pips, res.order, be_pips_cmd);

    _WriteResult(sig_id, (long)res.order, fill_price, "");
}

//==========================================================================
//  TRAILING STOP MANAGEMENT — helpers
//==========================================================================

// Returns the effective pip/point size for a symbol.
// Indices have no meaningful "pip" concept; use 1.0 (1 index point = $1).
// 5/3-digit forex: 10 × MT5 point. Commodities/stocks: raw point.
double _GetEffectivePipSize(string symbol, int digits, double point)
{
    if(StringFind(symbol, "US100") >= 0 || StringFind(symbol, "US30")  >= 0 ||
       StringFind(symbol, "US500") >= 0 || StringFind(symbol, "GER40") >= 0 ||
       StringFind(symbol, "UK100") >= 0)
        return 1.0;
    if(digits == 5 || digits == 3)
        return point * 10.0;
    return point;
}

// Per-symbol trailing distance (in pip_size units).
// For index symbols pip_size=1.0, so these are literal $ distances.
double _GetSymbolTrailPips(string symbol)
{
    if(StringFind(symbol, "US100") >= 0) return 15.0;   // $15 trail on Nasdaq
    if(StringFind(symbol, "US30")  >= 0) return 50.0;   // $50 trail on Dow
    return InpTrailingStopPips;                          // default for forex
}

// Per-symbol breakeven distance (same unit convention as _GetSymbolTrailPips).
double _GetSymbolBEPips(string symbol)
{
    if(StringFind(symbol, "US100") >= 0) return 25.0;   // $25 to breakeven on Nasdaq
    if(StringFind(symbol, "US30")  >= 0) return 80.0;   // $80 to breakeven on Dow
    return InpBreakevenPips;
}

//==========================================================================
//  TRAILING STOP MANAGEMENT
//==========================================================================

void _ManageTrailingStops()
{
    if(InpTrailingStopPips <= 0.0) return;

    // Collect currently open tickets under our magic number
    ulong open_tickets[];
    int total = PositionsTotal();
    ArrayResize(open_tickets, 0);

    for(int i = total - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(ticket == 0) continue;
        if(!PositionSelectByTicket(ticket)) continue;
        if((long)PositionGetInteger(POSITION_MAGIC) != (long)InpMagicNumber) continue;

        if(InpTrailOnlyScalps)
        {
            string pos_comment = PositionGetString(POSITION_COMMENT);
            if(StringFind(pos_comment, "scalp|") != 0) continue;
        }

        int n = ArraySize(open_tickets);
        ArrayResize(open_tickets, n + 1);
        open_tickets[n] = ticket;

        string symbol      = PositionGetString(POSITION_SYMBOL);
        long   pos_type    = PositionGetInteger(POSITION_TYPE);
        double open_price  = PositionGetDouble(POSITION_PRICE_OPEN);
        double current_sl  = PositionGetDouble(POSITION_SL);
        double current_tp  = PositionGetDouble(POSITION_TP);

        int    digits      = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
        double point       = SymbolInfoDouble(symbol, SYMBOL_POINT);
        double pip_size    = _GetEffectivePipSize(symbol, digits, point);

        // Per-ticket override (from Python ATR calculation) takes priority.
        // Falls back to the per-symbol default, then the global EA input.
        double trail_pips_val = _GetTicketVal(g_trail_tickets, g_trail_pips, ticket, 0.0);
        double be_pips_val    = _GetTicketVal(g_be_tickets,    g_be_pips,    ticket, 0.0);

        if(trail_pips_val <= 0.0) trail_pips_val = _GetSymbolTrailPips(symbol);
        if(be_pips_val    <= 0.0) be_pips_val    = _GetSymbolBEPips(symbol);

        double trail_dist  = trail_pips_val * pip_size;
        double be_dist     = be_pips_val    * pip_size;

        MqlTick tick;
        if(!SymbolInfoTick(symbol, tick)) continue;

        double new_sl = current_sl;

        if(pos_type == POSITION_TYPE_BUY)
        {
            double bid = tick.bid;

            if(be_pips_val > 0.0 && current_sl < open_price
               && bid >= open_price + be_dist)
            {
                new_sl = NormalizeDouble(open_price, digits);
            }

            double trail_sl = NormalizeDouble(bid - trail_dist, digits);
            if(trail_sl > new_sl)
                new_sl = trail_sl;
        }
        else // POSITION_TYPE_SELL
        {
            double ask = tick.ask;

            if(be_pips_val > 0.0 && current_sl > open_price
               && ask <= open_price - be_dist)
            {
                new_sl = NormalizeDouble(open_price, digits);
            }

            double trail_sl = NormalizeDouble(ask + trail_dist, digits);
            if(current_sl == 0.0 || trail_sl < new_sl)
                new_sl = trail_sl;
        }

        if(new_sl == current_sl || new_sl <= 0.0) continue;

        MqlTradeRequest req = {};
        MqlTradeResult  res = {};

        req.action   = TRADE_ACTION_SLTP;
        req.position = ticket;
        req.symbol   = symbol;
        req.sl       = new_sl;
        req.tp       = current_tp;
        req.magic    = (ulong)InpMagicNumber;

        if(!OrderSend(req, res))
        {
            Print("AIBotScalper: trail modify failed ticket=", ticket,
                  " retcode=", res.retcode);
        }
    }

    // Clean up stored values for tickets that are no longer open
    int stored = ArraySize(g_trail_tickets);
    for(int i = stored - 1; i >= 0; i--)
    {
        ulong t = g_trail_tickets[i];
        bool found = false;
        int ot = ArraySize(open_tickets);
        for(int j = 0; j < ot; j++)
            if(open_tickets[j] == t) { found = true; break; }
        if(!found)
            _RemoveTicket(t);
    }
}

//==========================================================================
//  RESULT FILE WRITING
//==========================================================================

void _WriteResult(
    const string sig_id,
    const long   ticket,
    const double fill_price,
    const string error)
{
    string fname = RES_PREFIX + sig_id + ".json";
    int fh = FileOpen(fname, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
    if(fh == INVALID_HANDLE)
    {
        Print("AIBotScalper: cannot write result for [", sig_id, "]");
        return;
    }

    string status = (error == "") ? "ok" : "error";
    string json   = "{"
        + "\"id\":\""          + sig_id                              + "\","
        + "\"status\":\""      + status                              + "\","
        + "\"ticket\":"        + IntegerToString(ticket)             + ","
        + "\"fill_price\":"    + DoubleToString(fill_price, 6)       + ","
        + "\"error\":\""       + error                               + "\","
        + "\"timestamp\":"     + IntegerToString((long)TimeCurrent())
        + "}";

    FileWriteString(fh, json);
    FileClose(fh);
}

//==========================================================================
//  MINIMAL JSON HELPERS
//  (MQL5 has no built-in JSON parser — these handle flat single-level objects)
//==========================================================================

// Extract a string value: "key":"value"
string _JsonStr(const string json, const string key)
{
    string search = "\"" + key + "\":\"";
    int pos = StringFind(json, search);
    if(pos < 0) return "";
    pos += StringLen(search);
    int end = StringFind(json, "\"", pos);
    if(end < 0) return "";
    return StringSubstr(json, pos, end - pos);
}

// Extract a numeric value: "key":123.456
double _JsonNum(const string json, const string key)
{
    string search = "\"" + key + "\":";
    int pos = StringFind(json, search);
    if(pos < 0) return 0.0;
    pos += StringLen(search);
    // Skip optional opening quote (for string-encoded numbers)
    if(StringGetCharacter(json, pos) == '"') pos++;
    // Find end of value
    int end = pos;
    int len = StringLen(json);
    while(end < len)
    {
        ushort c = StringGetCharacter(json, end);
        if(c == ',' || c == '}' || c == '"' || c == '\r' || c == '\n') break;
        end++;
    }
    return StringToDouble(StringSubstr(json, pos, end - pos));
}
//+------------------------------------------------------------------+
