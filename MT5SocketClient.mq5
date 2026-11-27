//+------------------------------------------------------------------+
//|                                              MT5SocketClient.mq5 |
//|                                     MT5-Python Socket Bridge EA  |
//|                         Uses Windows Winsock for reliable sockets |
//+------------------------------------------------------------------+
#property copyright "MT5-Python Bridge"
#property version   "2.00"
#property strict

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>
#include <Trade\OrderInfo.mqh>
#include <Trade\AccountInfo.mqh>

//--- Winsock DLL imports
#import "ws2_32.dll"
   int WSAStartup(int wVersionRequested, int &lpWSAData[]);
   int WSACleanup();
   int WSAGetLastError();
   uint socket(int af, int type, int protocol);
   int closesocket(uint s);
   int connect(uint s, uchar &name[], int namelen);
   int send(uint s, uchar &buf[], int len, int flags);
   int recv(uint s, uchar &buf[], int len, int flags);
   int ioctlsocket(uint s, uint cmd, uint &argp);
   uint htons(uint hostshort);
   ulong inet_addr(uchar &cp[]);
#import

//--- Winsock constants
#define AF_INET         2
#define SOCK_STREAM     1
#define IPPROTO_TCP     6
#define INVALID_SOCKET  0xFFFFFFFF
#define SOCKET_ERROR    -1
#define FIONBIO         0x8004667E

//--- Input parameters
input string   ServerAddress = "127.0.0.1";       // Hermes IPs, comma-separated
input int      ServerPort = 5555;                  // Python server port (shared)
input int      ReconnectDelayMs = 5000;            // Reconnect delay (ms)
input int      HeartbeatIntervalMs = 10000;        // Heartbeat interval (ms)
input int      TimerIntervalMs = 10;               // Polling interval (ms)

//--- Global variables
uint           g_socket = INVALID_SOCKET;
bool           g_connected = false;
bool           g_wsaInitialized = false;
datetime       g_lastHeartbeat = 0;
datetime       g_lastReconnectAttempt = 0;
string         g_receiveBuffer = "";
CTrade         g_trade;
CPositionInfo  g_position;
COrderInfo     g_order;
CAccountInfo   g_account;
ulong          g_t2_receive = 0;  // Timestamp when message was received (for latency measurement)

//+------------------------------------------------------------------+
//| Expert initialization function                                     |
//+------------------------------------------------------------------+
int OnInit()
{
    Print("MT5SocketClient v2.0 initializing (Winsock)...");
    Print("Server: ", ServerAddress, ":", ServerPort);

    // Initialize Winsock
    int wsaData[100];
    int result = WSAStartup(0x0202, wsaData);  // Winsock 2.2
    if(result != 0)
    {
        Print("WSAStartup failed: ", result);
        return(INIT_FAILED);
    }
    g_wsaInitialized = true;
    Print("Winsock initialized");

    // Set up trade object
    g_trade.SetExpertMagicNumber(0);
    g_trade.SetDeviationInPoints(10);
    g_trade.SetTypeFilling(ORDER_FILLING_IOC);

    // Start timer for polling
    EventSetMillisecondTimer(TimerIntervalMs);

    // Attempt initial connection
    ConnectToServer();

    return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                   |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    EventKillTimer();
    Disconnect();

    if(g_wsaInitialized)
    {
        WSACleanup();
        g_wsaInitialized = false;
    }

    Print("MT5SocketClient stopped. Reason: ", reason);
}

//+------------------------------------------------------------------+
//| Timer function - main polling loop                                 |
//+------------------------------------------------------------------+
void OnTimer()
{
    // Check connection status
    if(!g_connected)
    {
        // Rate-limit reconnection attempts
        if(TimeCurrent() - g_lastReconnectAttempt >= ReconnectDelayMs / 1000)
        {
            ConnectToServer();
        }
        return;
    }

    // Read incoming data
    ReadFromSocket();

    // Process complete messages
    ProcessMessages();

    // Send heartbeat if needed
    CheckHeartbeat();
}

//+------------------------------------------------------------------+
//| Connect to Python server using Winsock                             |
//+------------------------------------------------------------------+
string TrimCopy(string s)
{
    while(StringLen(s) > 0 && StringGetCharacter(s, 0) == ' ')
        s = StringSubstr(s, 1);
    while(StringLen(s) > 0 && StringGetCharacter(s, StringLen(s) - 1) == ' ')
        s = StringSubstr(s, 0, StringLen(s) - 1);
    return s;
}

bool ConnectToAddress(string ip)
{
    g_socket = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if(g_socket == INVALID_SOCKET)
    {
        Print("Socket creation failed: ", WSAGetLastError());
        return false;
    }

    uchar sockAddr[16];
    ArrayInitialize(sockAddr, 0);
    sockAddr[0] = AF_INET & 0xFF;
    sockAddr[1] = (AF_INET >> 8) & 0xFF;

    uint port = htons((uint)ServerPort);
    sockAddr[2] = (uchar)(port & 0xFF);
    sockAddr[3] = (uchar)((port >> 8) & 0xFF);

    uchar ipAddr[];
    StringToCharArray(ip, ipAddr);
    ulong addr = inet_addr(ipAddr);
    sockAddr[4] = (uchar)(addr & 0xFF);
    sockAddr[5] = (uchar)((addr >> 8) & 0xFF);
    sockAddr[6] = (uchar)((addr >> 16) & 0xFF);
    sockAddr[7] = (uchar)((addr >> 24) & 0xFF);

    if(connect(g_socket, sockAddr, 16) == SOCKET_ERROR)
    {
        int err = WSAGetLastError();
        Print("Connect failed ", ip, ":", ServerPort, " err=", err);
        closesocket(g_socket);
        g_socket = INVALID_SOCKET;
        return false;
    }

    uint nonBlocking = 1;
    ioctlsocket(g_socket, FIONBIO, nonBlocking);
    g_connected = true;
    g_lastHeartbeat = TimeCurrent();
    g_receiveBuffer = "";
    Print("Connected to Python server at ", ip, ":", ServerPort);
    return true;
}

bool ConnectToServer()
{
    g_lastReconnectAttempt = TimeCurrent();

    if(g_connected)
        return true;

    string list = ServerAddress;
    int start = 0;
    while(true)
    {
        int comma = StringFind(list, ",", start);
        string ip;
        if(comma < 0)
            ip = StringSubstr(list, start);
        else
            ip = StringSubstr(list, start, comma - start);
        ip = TrimCopy(ip);
        if(StringLen(ip) > 0)
        {
            if(ConnectToAddress(ip))
                return true;
        }
        if(comma < 0)
            break;
        start = comma + 1;
    }
    return false;
}

//+------------------------------------------------------------------+
//| Disconnect from server                                             |
//+------------------------------------------------------------------+
void Disconnect()
{
    if(g_socket != INVALID_SOCKET)
    {
        closesocket(g_socket);
        g_socket = INVALID_SOCKET;
    }
    g_connected = false;
    g_receiveBuffer = "";
    Print("Disconnected from server");
}

//+------------------------------------------------------------------+
//| Read data from socket into buffer                                  |
//+------------------------------------------------------------------+
void ReadFromSocket()
{
    if(!g_connected || g_socket == INVALID_SOCKET)
        return;

    uchar buffer[4096];
    ArrayInitialize(buffer, 0);

    int bytesRead = recv(g_socket, buffer, 4096, 0);

    if(bytesRead > 0)
    {
        string data = CharArrayToString(buffer, 0, bytesRead);
        g_receiveBuffer += data;
        Print("Received ", bytesRead, " bytes");
    }
    else if(bytesRead == 0)
    {
        // Connection closed gracefully
        Print("Server closed connection");
        Disconnect();
    }
    else
    {
        // Error or would block
        int err = WSAGetLastError();
        // 10035 = WSAEWOULDBLOCK (no data available, normal for non-blocking)
        if(err != 10035)
        {
            Print("Recv error: ", err);
            Disconnect();
        }
    }
}

//+------------------------------------------------------------------+
//| Process complete messages in buffer                                |
//+------------------------------------------------------------------+
void ProcessMessages()
{
    int pos;
    while((pos = StringFind(g_receiveBuffer, "\n")) >= 0)
    {
        string message = StringSubstr(g_receiveBuffer, 0, pos);
        g_receiveBuffer = StringSubstr(g_receiveBuffer, pos + 1);

        if(StringLen(message) > 0)
        {
            HandleMessage(message);
        }
    }
}

//+------------------------------------------------------------------+
//| Handle a single JSON message                                       |
//+------------------------------------------------------------------+
void HandleMessage(string json)
{
    // Capture receive timestamp for latency measurement
    g_t2_receive = GetMicrosecondCount();

    // Parse JSON fields
    string requestId = GetJsonString(json, "id");
    string action = GetJsonString(json, "action");
    string params = GetJsonObject(json, "params");

    Print("Received command: ", action, " [", requestId, "]");

    string response = "";

    if(action == "TRADE")
        response = HandleTrade(requestId, params);
    else if(action == "GET_DATA")
        response = HandleGetData(requestId, params);
    else if(action == "GET_TICK")
        response = HandleGetTick(requestId, params);
    else if(action == "GET_ACCOUNT")
        response = HandleGetAccount(requestId);
    else if(action == "GET_POSITIONS")
        response = HandleGetPositions(requestId);
    else if(action == "CLOSE_POSITION")
        response = HandleClosePosition(requestId, params);
    else if(action == "HEARTBEAT")
        response = HandleHeartbeat(requestId);
    else if(action == "GET_SYMBOLS")
        response = HandleGetSymbols(requestId);
    else if(action == "GET_SYMBOL_INFO")
        response = HandleGetSymbolInfo(requestId, params);
    else if(action == "GET_ORDERS")
        response = HandleGetOrders(requestId);
    else if(action == "MODIFY_POSITION")
        response = HandleModifyPosition(requestId, params);
    else if(action == "CANCEL_ORDER")
        response = HandleCancelOrder(requestId, params);
    else if(action == "MODIFY_ORDER")
        response = HandleModifyOrder(requestId, params);
    else
        response = BuildErrorResponse(requestId, -1, "Unknown action: " + action);

    SendResponse(response);
}

//+------------------------------------------------------------------+
//| Handle TRADE command                                               |
//+------------------------------------------------------------------+
string HandleTrade(string requestId, string params)
{
    string symbol = GetJsonString(params, "symbol");
    string typeStr = GetJsonString(params, "type");
    double volume = GetJsonDouble(params, "volume");
    double sl = GetJsonDouble(params, "sl");
    double tp = GetJsonDouble(params, "tp");
    double price = GetJsonDouble(params, "price");
    int magic = (int)GetJsonDouble(params, "magic");
    string comment = GetJsonString(params, "comment");

    // Validate symbol
    if(!SymbolSelect(symbol, true))
    {
        return BuildErrorResponse(requestId, GetLastError(), "Invalid symbol: " + symbol);
    }

    // Get current prices
    MqlTick tick;
    if(!SymbolInfoTick(symbol, tick))
    {
        return BuildErrorResponse(requestId, GetLastError(), "Failed to get tick for " + symbol);
    }

    // Set magic number
    if(magic > 0)
        g_trade.SetExpertMagicNumber(magic);

    // Determine order type and execute
    bool result = false;

    if(typeStr == "BUY")
    {
        result = g_trade.Buy(volume, symbol, tick.ask, sl, tp, comment);
    }
    else if(typeStr == "SELL")
    {
        result = g_trade.Sell(volume, symbol, tick.bid, sl, tp, comment);
    }
    else if(typeStr == "BUY_LIMIT")
    {
        result = g_trade.BuyLimit(volume, price, symbol, sl, tp, ORDER_TIME_GTC, 0, comment);
    }
    else if(typeStr == "SELL_LIMIT")
    {
        result = g_trade.SellLimit(volume, price, symbol, sl, tp, ORDER_TIME_GTC, 0, comment);
    }
    else if(typeStr == "BUY_STOP")
    {
        result = g_trade.BuyStop(volume, price, symbol, sl, tp, ORDER_TIME_GTC, 0, comment);
    }
    else if(typeStr == "SELL_STOP")
    {
        result = g_trade.SellStop(volume, price, symbol, sl, tp, ORDER_TIME_GTC, 0, comment);
    }
    else
    {
        return BuildErrorResponse(requestId, -1, "Unknown order type: " + typeStr);
    }

    if(!result)
    {
        return BuildErrorResponse(requestId, (int)g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
    }

    // Build success response
    string data = StringFormat(
        "{\"ticket\":\"%I64u\",\"price_executed\":%.5f,\"volume_executed\":%.2f}",
        g_trade.ResultDeal(),
        g_trade.ResultPrice(),
        g_trade.ResultVolume()
    );

    return BuildSuccessResponse(requestId, data);
}

//+------------------------------------------------------------------+
//| Handle GET_DATA command                                            |
//+------------------------------------------------------------------+
string HandleGetData(string requestId, string params)
{
    string symbol = GetJsonString(params, "symbol");
    string tfStr = GetJsonString(params, "timeframe");
    int count = (int)GetJsonDouble(params, "count");

    // Validate symbol
    if(!SymbolSelect(symbol, true))
    {
        return BuildErrorResponse(requestId, GetLastError(), "Invalid symbol: " + symbol);
    }

    // Map timeframe string to enum
    ENUM_TIMEFRAMES timeframe = StringToTimeframe(tfStr);
    if(timeframe == PERIOD_CURRENT)
    {
        return BuildErrorResponse(requestId, -1, "Invalid timeframe: " + tfStr);
    }

    // Get rates
    MqlRates rates[];
    ArraySetAsSeries(rates, true);
    int copied = CopyRates(symbol, timeframe, 0, count, rates);

    if(copied <= 0)
    {
        return BuildErrorResponse(requestId, GetLastError(), "Failed to copy rates");
    }

    // Build rates array JSON
    string ratesJson = "[";
    for(int i = 0; i < copied; i++)
    {
        if(i > 0) ratesJson += ",";
        ratesJson += StringFormat(
            "{\"time\":\"%s\",\"open\":%.5f,\"high\":%.5f,\"low\":%.5f,\"close\":%.5f,\"volume\":%d}",
            TimeToString(rates[i].time, TIME_DATE|TIME_SECONDS),
            rates[i].open,
            rates[i].high,
            rates[i].low,
            rates[i].close,
            rates[i].tick_volume
        );
    }
    ratesJson += "]";

    string data = "{\"rates\":" + ratesJson + "}";
    return BuildSuccessResponse(requestId, data);
}

//+------------------------------------------------------------------+
//| Handle GET_TICK command                                            |
//+------------------------------------------------------------------+
string HandleGetTick(string requestId, string params)
{
    string symbol = GetJsonString(params, "symbol");

    // Validate symbol
    if(!SymbolSelect(symbol, true))
    {
        return BuildErrorResponse(requestId, GetLastError(), "Invalid symbol: " + symbol);
    }

    MqlTick tick;
    if(!SymbolInfoTick(symbol, tick))
    {
        return BuildErrorResponse(requestId, GetLastError(), "Failed to get tick");
    }

    string data = StringFormat(
        "{\"bid\":%.5f,\"ask\":%.5f,\"last\":%.5f,\"volume\":%d,\"time\":\"%s\"}",
        tick.bid,
        tick.ask,
        tick.last,
        tick.volume,
        TimeToString(tick.time, TIME_DATE|TIME_SECONDS)
    );

    return BuildSuccessResponse(requestId, data);
}

//+------------------------------------------------------------------+
//| Handle GET_ACCOUNT command                                         |
//+------------------------------------------------------------------+
string HandleGetAccount(string requestId)
{
    string data = StringFormat(
        "{\"balance\":%.2f,\"equity\":%.2f,\"margin\":%.2f,\"free_margin\":%.2f,\"leverage\":%d,\"login\":%s}",
        g_account.Balance(),
        g_account.Equity(),
        g_account.Margin(),
        g_account.FreeMargin(),
        g_account.Leverage(),
        IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN))
    );

    return BuildSuccessResponse(requestId, data);
}

//+------------------------------------------------------------------+
//| Handle GET_POSITIONS command                                       |
//+------------------------------------------------------------------+
string HandleGetPositions(string requestId)
{
    string positionsJson = "[";
    bool first = true;

    for(int i = 0; i < PositionsTotal(); i++)
    {
        if(g_position.SelectByIndex(i))
        {
            if(!first) positionsJson += ",";
            first = false;

            string posType = (g_position.PositionType() == POSITION_TYPE_BUY) ? "BUY" : "SELL";

            positionsJson += StringFormat(
                "{\"ticket\":\"%I64u\",\"symbol\":\"%s\",\"type\":\"%s\",\"volume\":%.2f,\"open_price\":%.5f,\"sl\":%.5f,\"tp\":%.5f,\"profit\":%.2f}",
                g_position.Ticket(),
                g_position.Symbol(),
                posType,
                g_position.Volume(),
                g_position.PriceOpen(),
                g_position.StopLoss(),
                g_position.TakeProfit(),
                g_position.Profit()
            );
        }
    }
    positionsJson += "]";

    string data = "{\"positions\":" + positionsJson + "}";
    return BuildSuccessResponse(requestId, data);
}

//+------------------------------------------------------------------+
//| Handle CLOSE_POSITION command                                      |
//+------------------------------------------------------------------+
string HandleClosePosition(string requestId, string params)
{
    ulong ticket = GetJsonUlong(params, "ticket");

    if(!g_position.SelectByTicket(ticket))
    {
        return BuildErrorResponse(requestId, GetLastError(), "Position not found: " + StringFormat("%I64u", ticket));
    }

    double closePrice = (g_position.PositionType() == POSITION_TYPE_BUY)
                        ? SymbolInfoDouble(g_position.Symbol(), SYMBOL_BID)
                        : SymbolInfoDouble(g_position.Symbol(), SYMBOL_ASK);

    double profit = g_position.Profit();

    if(!g_trade.PositionClose(ticket))
    {
        return BuildErrorResponse(requestId, (int)g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
    }

    string data = StringFormat(
        "{\"close_price\":%.5f,\"profit\":%.2f}",
        closePrice,
        profit
    );

    return BuildSuccessResponse(requestId, data);
}

//+------------------------------------------------------------------+
//| Handle HEARTBEAT command                                           |
//+------------------------------------------------------------------+
string HandleHeartbeat(string requestId)
{
    g_lastHeartbeat = TimeCurrent();

    string data = StringFormat(
        "{\"timestamp\":\"%s\",\"connected\":true}",
        TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS)
    );

    return BuildSuccessResponse(requestId, data);
}

//+------------------------------------------------------------------+
//| Handle GET_SYMBOLS command                                         |
//+------------------------------------------------------------------+
string HandleGetSymbols(string requestId)
{
    string symbolsJson = "[";
    int total = SymbolsTotal(true);  // true = only Market Watch symbols
    bool first = true;

    for(int i = 0; i < total; i++)
    {
        string sym = SymbolName(i, true);
        if(StringLen(sym) > 0)
        {
            if(!first) symbolsJson += ",";
            first = false;
            symbolsJson += "\"" + sym + "\"";
        }
    }
    symbolsJson += "]";

    string data = "{\"symbols\":" + symbolsJson + "}";
    return BuildSuccessResponse(requestId, data);
}

//+------------------------------------------------------------------+
//| Handle GET_SYMBOL_INFO command                                     |
//+------------------------------------------------------------------+
string HandleGetSymbolInfo(string requestId, string params)
{
    string symbol = GetJsonString(params, "symbol");
    if(StringLen(symbol) == 0)
        return BuildErrorResponse(requestId, -1, "Symbol is required");
    if(!SymbolSelect(symbol, true))
        return BuildErrorResponse(requestId, GetLastError(), "Invalid symbol: " + symbol);

    string jsonSymbol = symbol;
    string base = SymbolInfoString(symbol, SYMBOL_CURRENCY_BASE);
    StringReplace(jsonSymbol, "\"", "");
    StringReplace(base, "\"", "");

    string data = StringFormat(
        "{\"symbol\":\"%s\",\"contract_size\":%.8f,\"volume_min\":%.8f,\"volume_max\":%.8f,\"volume_step\":%.8f,\"digits\":%d,\"point\":%.10f,\"tick_size\":%.10f,\"stops_level\":%d,\"freeze_level\":%d,\"currency_base\":\"%s\"}",
        jsonSymbol,
        SymbolInfoDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE),
        SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN),
        SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX),
        SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP),
        (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS),
        SymbolInfoDouble(symbol, SYMBOL_POINT),
        SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE),
        (int)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL),
        (int)SymbolInfoInteger(symbol, SYMBOL_TRADE_FREEZE_LEVEL),
        base
    );
    return BuildSuccessResponse(requestId, data);
}

string OrderTypeName(ENUM_ORDER_TYPE t)
{
    if(t == ORDER_TYPE_BUY) return "BUY";
    if(t == ORDER_TYPE_SELL) return "SELL";
    if(t == ORDER_TYPE_BUY_LIMIT) return "BUY_LIMIT";
    if(t == ORDER_TYPE_SELL_LIMIT) return "SELL_LIMIT";
    if(t == ORDER_TYPE_BUY_STOP) return "BUY_STOP";
    if(t == ORDER_TYPE_SELL_STOP) return "SELL_STOP";
    if(t == ORDER_TYPE_BUY_STOP_LIMIT) return "BUY_STOP_LIMIT";
    if(t == ORDER_TYPE_SELL_STOP_LIMIT) return "SELL_STOP_LIMIT";
    return "UNKNOWN";
}

bool JsonHasKey(string json, string key)
{
    string a = "\"" + key + "\":";
    string b = "\"" + key + "\": ";
    return (StringFind(json, a) >= 0 || StringFind(json, b) >= 0);
}

string HandleGetOrders(string requestId)
{
    string ordersJson = "[";
    bool first = true;
    for(int i = 0; i < OrdersTotal(); i++)
    {
        if(!g_order.SelectByIndex(i))
            continue;
        if(!first) ordersJson += ",";
        first = false;
        ordersJson += StringFormat(
            "{\"ticket\":\"%I64u\",\"symbol\":\"%s\",\"type\":\"%s\",\"volume\":%.8f,\"price\":%.8f,\"sl\":%.8f,\"tp\":%.8f}",
            g_order.Ticket(),
            g_order.Symbol(),
            OrderTypeName(g_order.OrderType()),
            g_order.VolumeCurrent(),
            g_order.PriceOpen(),
            g_order.StopLoss(),
            g_order.TakeProfit()
        );
    }
    ordersJson += "]";
    string data = "{\"orders\":" + ordersJson + "}";
    return BuildSuccessResponse(requestId, data);
}

string HandleModifyPosition(string requestId, string params)
{
    ulong ticket = GetJsonUlong(params, "ticket");
    if(!g_position.SelectByTicket(ticket))
        return BuildErrorResponse(requestId, GetLastError(), "Position not found: " + StringFormat("%I64u", ticket));
    double sl = g_position.StopLoss();
    double tp = g_position.TakeProfit();
    if(JsonHasKey(params, "sl"))
        sl = GetJsonDouble(params, "sl");
    if(JsonHasKey(params, "tp"))
        tp = GetJsonDouble(params, "tp");
    if(!g_trade.PositionModify(ticket, sl, tp))
        return BuildErrorResponse(requestId, (int)g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
    string data = StringFormat("{\"ticket\":\"%I64u\",\"sl\":%.8f,\"tp\":%.8f}", ticket, sl, tp);
    return BuildSuccessResponse(requestId, data);
}

string HandleCancelOrder(string requestId, string params)
{
    ulong ticket = GetJsonUlong(params, "ticket");
    if(!g_order.Select(ticket))
        return BuildErrorResponse(requestId, GetLastError(), "Order not found: " + StringFormat("%I64u", ticket));
    if(!g_trade.OrderDelete(ticket))
        return BuildErrorResponse(requestId, (int)g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
    string data = StringFormat("{\"ticket\":\"%I64u\",\"cancelled\":true}", ticket);
    return BuildSuccessResponse(requestId, data);
}

string HandleModifyOrder(string requestId, string params)
{
    ulong ticket = GetJsonUlong(params, "ticket");
    if(!g_order.Select(ticket))
        return BuildErrorResponse(requestId, GetLastError(), "Order not found: " + StringFormat("%I64u", ticket));
    double price = g_order.PriceOpen();
    double sl = g_order.StopLoss();
    double tp = g_order.TakeProfit();
    double volume = g_order.VolumeCurrent();
    if(JsonHasKey(params, "price"))
        price = GetJsonDouble(params, "price");
    if(JsonHasKey(params, "sl"))
        sl = GetJsonDouble(params, "sl");
    if(JsonHasKey(params, "tp"))
        tp = GetJsonDouble(params, "tp");
    if(JsonHasKey(params, "volume"))
        volume = GetJsonDouble(params, "volume");
    if(!g_trade.OrderModify(ticket, price, sl, tp, ORDER_TIME_GTC, 0))
        return BuildErrorResponse(requestId, (int)g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
    string data = StringFormat(
        "{\"ticket\":\"%I64u\",\"price\":%.8f,\"sl\":%.8f,\"tp\":%.8f,\"volume\":%.8f}",
        ticket, price, sl, tp, volume
    );
    return BuildSuccessResponse(requestId, data);
}

//+------------------------------------------------------------------+
//| Check and send heartbeat if needed                                 |
//+------------------------------------------------------------------+
void CheckHeartbeat()
{
    if(TimeCurrent() - g_lastHeartbeat > HeartbeatIntervalMs / 1000)
    {
        g_lastHeartbeat = TimeCurrent();
    }
}

//+------------------------------------------------------------------+
//| Send response to Python server                                     |
//+------------------------------------------------------------------+
void SendResponse(string response)
{
    if(!g_connected || g_socket == INVALID_SOCKET)
        return;

    response += "\n";  // Add message terminator

    uchar data[];
    int len = StringToCharArray(response, data, 0, WHOLE_ARRAY, CP_UTF8);

    // Remove null terminator from length
    if(len > 0) len--;

    int sent = send(g_socket, data, len, 0);
    if(sent == SOCKET_ERROR)
    {
        int err = WSAGetLastError();
        Print("Send failed: ", err);
        Disconnect();
    }
    else
    {
        Print("Sent ", sent, " bytes");
    }
}

//+------------------------------------------------------------------+
//| Build success response JSON                                        |
//+------------------------------------------------------------------+
string BuildSuccessResponse(string requestId, string data)
{
    // Capture execute complete timestamp for latency measurement
    ulong t3_execute = GetMicrosecondCount();

    return StringFormat(
        "{\"id\":\"%s\",\"success\":true,\"error_code\":0,\"error_message\":\"\",\"data\":%s,\"timing\":{\"t2_receive\":%I64u,\"t3_execute\":%I64u}}",
        requestId,
        data,
        g_t2_receive,
        t3_execute
    );
}

//+------------------------------------------------------------------+
//| Build error response JSON                                          |
//+------------------------------------------------------------------+
string BuildErrorResponse(string requestId, int errorCode, string errorMessage)
{
    // Escape quotes in error message
    StringReplace(errorMessage, "\"", "\\\"");

    return StringFormat(
        "{\"id\":\"%s\",\"success\":false,\"error_code\":%d,\"error_message\":\"%s\",\"data\":{}}",
        requestId,
        errorCode,
        errorMessage
    );
}

//+------------------------------------------------------------------+
//| Convert timeframe string to enum                                   |
//+------------------------------------------------------------------+
ENUM_TIMEFRAMES StringToTimeframe(string tf)
{
    if(tf == "M1")  return PERIOD_M1;
    if(tf == "M5")  return PERIOD_M5;
    if(tf == "M15") return PERIOD_M15;
    if(tf == "M30") return PERIOD_M30;
    if(tf == "H1")  return PERIOD_H1;
    if(tf == "H4")  return PERIOD_H4;
    if(tf == "D1")  return PERIOD_D1;
    if(tf == "W1")  return PERIOD_W1;
    if(tf == "MN1") return PERIOD_MN1;
    return PERIOD_CURRENT;  // Invalid
}

//+------------------------------------------------------------------+
//| Simple JSON string value parser                                    |
//+------------------------------------------------------------------+
string GetJsonString(string json, string key)
{
    // Try without space first: "key":"value"
    string searchKey = "\"" + key + "\":\"";
    int start = StringFind(json, searchKey);

    // Try with space: "key": "value"
    if(start < 0)
    {
        searchKey = "\"" + key + "\": \"";
        start = StringFind(json, searchKey);
    }

    if(start < 0) return "";

    start += StringLen(searchKey);
    int end = StringFind(json, "\"", start);
    if(end < 0) return "";

    return StringSubstr(json, start, end - start);
}

//+------------------------------------------------------------------+
//| Simple JSON number value parser                                    |
//+------------------------------------------------------------------+
double GetJsonDouble(string json, string key)
{
    // Find key position
    string searchKey = "\"" + key + "\":";
    int keyPos = StringFind(json, searchKey);

    // Try with space
    if(keyPos < 0)
    {
        searchKey = "\"" + key + "\": ";
        keyPos = StringFind(json, searchKey);
    }

    if(keyPos < 0) return 0;

    int start = keyPos + StringLen(searchKey);

    // Skip whitespace
    while(start < StringLen(json) && StringGetCharacter(json, start) == ' ')
        start++;

    // Check if quoted number
    if(StringGetCharacter(json, start) == '"')
    {
        start++;
        int end = StringFind(json, "\"", start);
        if(end > start)
            return StringToDouble(StringSubstr(json, start, end - start));
        return 0;
    }

    // Unquoted number - find end
    int end = start;
    while(end < StringLen(json))
    {
        ushort c = StringGetCharacter(json, end);
        if(c != '-' && c != '.' && (c < '0' || c > '9'))
            break;
        end++;
    }

    if(end > start)
        return StringToDouble(StringSubstr(json, start, end - start));

    return 0;
}

ulong GetJsonUlong(string json, string key)
{
    string searchKey = "\"" + key + "\":";
    int keyPos = StringFind(json, searchKey);
    if(keyPos < 0)
    {
        searchKey = "\"" + key + "\": ";
        keyPos = StringFind(json, searchKey);
    }
    if(keyPos < 0) return 0;

    int start = keyPos + StringLen(searchKey);
    while(start < StringLen(json) && StringGetCharacter(json, start) == ' ')
        start++;
    if(StringGetCharacter(json, start) == '"')
        start++;
    int end = start;
    while(end < StringLen(json))
    {
        ushort c = StringGetCharacter(json, end);
        if(c < '0' || c > '9')
            break;
        end++;
    }
    if(end <= start) return 0;
    ulong value = 0;
    for(int i = start; i < end; i++)
        value = value * 10 + (ulong)(StringGetCharacter(json, i) - '0');
    return value;
}

//+------------------------------------------------------------------+
//| Simple JSON object value parser                                    |
//+------------------------------------------------------------------+
string GetJsonObject(string json, string key)
{
    string searchKey = "\"" + key + "\":";
    int start = StringFind(json, searchKey);

    // Try with space
    if(start < 0)
    {
        searchKey = "\"" + key + "\": ";
        start = StringFind(json, searchKey);
    }

    if(start < 0) return "{}";

    start += StringLen(searchKey);

    // Skip whitespace
    while(start < StringLen(json) && StringGetCharacter(json, start) == ' ')
        start++;

    if(StringGetCharacter(json, start) != '{')
        return "{}";

    // Find matching closing brace
    int depth = 0;
    int end = start;
    while(end < StringLen(json))
    {
        ushort c = StringGetCharacter(json, end);
        if(c == '{') depth++;
        if(c == '}') depth--;
        end++;
        if(depth == 0) break;
    }

    return StringSubstr(json, start, end - start);
}

//+------------------------------------------------------------------+
//| OnTick - not used for socket communication                         |
//+------------------------------------------------------------------+
void OnTick()
{
    // Socket communication handled in OnTimer
}
//+------------------------------------------------------------------+
