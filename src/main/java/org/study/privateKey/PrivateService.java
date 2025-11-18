package org.study.privateKey;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.util.LinkedMultiValueMap;
import org.springframework.util.MultiValueMap;
import org.study.common.CustomException;
import org.study.common.HttpService;
import org.study.config.AuthToken;
import org.study.privateKey.accounts.Accounts;
import org.study.privateKey.accounts.AccountsDto;
import org.study.publicKey.marketAll.MarketCode;
import org.study.publicKey.PublicService;
import org.study.publicKey.orders.Orders;
import org.study.publicKey.orders.OrdersDto;
import org.study.publicKey.ticker.Ticker;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.stream.Collectors;

/**
 * 1. ClassName     : PrivateService
 * 2. FileName      : PrivateService.java
 * 3. Package       : org.study.privateKey
 * 4. Author        : 윤명석
 * 5. Creation Date : 25. 11. 16. 오후 8:10
 * 6. Modified Date : 25. 11. 16. 오후 8:10
 * 7. Comment       :
 */
@Service
@Slf4j
@RequiredArgsConstructor
public class PrivateService {
    
    private final PublicService publicService;
    private final ObjectMapper objectMapper = new ObjectMapper();
    private final HttpService httpService;
    private final AuthToken authToken;
    
    public List<AccountsDto> accounts() {
        try {
            String authenticationToken = authToken.getAuthToken();
            MultiValueMap<String, String> headers = new LinkedMultiValueMap<>();
            headers.add("Authorization", authenticationToken);
            
            MultiValueMap<String, String> params = new LinkedMultiValueMap<>();
            
            String response = httpService.client("get", "accounts", headers, params);
            
            // ✅ JSON → 객체
            List<Accounts> accounts = objectMapper.readValue(
                response,
                new TypeReference<>() {
                }
            );
            
            Map<String, MarketCode> marketByCurrency =
                publicService.marketAll().stream()
                    .filter(mc -> mc.getMarket() != null && mc.getMarket().length() > 4)
                    .collect(Collectors.toMap(
                        mc -> mc.getMarket().substring(4),   // BTC, ETH ...
                        Function.identity(),
                        (a, b) -> a // key 중복 시 첫 번째 것 유지
                    ));
            log.info("전체계좌: {}", accounts);
            List<AccountsDto> accountsDtoList = new ArrayList<>();
            for (Accounts account : accounts) {
                String currency = account.getCurrency();
                AccountsDto dto = null;
                
                // 1) 포인트(P)
                if ("P".equals(currency)) {
                    dto = AccountsDto.builder()
                        .balance(account.getBalance())
                        .engName("P")
                        .korName("포인트")
                        .build();
                }
                // 2) 원화(KRW)
                else if ("KRW".equals(currency)) {
                    dto = AccountsDto.builder()
                        .balance(account.getBalance())
                        .engName("KRW")
                        .korName("원화")
                        .build();
                }
                // 3) 나머지 코인들 (BTC, ETH 등)
                else {
                    MarketCode marketCode = marketByCurrency.get(currency);
                    if (marketCode != null) {
                        // locked + balance 합산
                        BigDecimal balance = new BigDecimal(account.getLocked())
                            .add(new BigDecimal(account.getBalance()));
                        
                        double avgPrice = account.getAvg_buy_price();
                        BigDecimal avgPriceBD = BigDecimal.valueOf(avgPrice);
                        
                        int buyAmount = balance
                            .multiply(avgPriceBD)
                            .setScale(0, RoundingMode.HALF_UP)
                            .intValue();
                        
                        Ticker ticker = publicService.ticker(account.getCurrency()).getFirst();
                        int estimatedValue = balance.multiply(ticker.getTrade_price()).intValue();
                        int unrealizedPnL = estimatedValue - buyAmount;
                        double rateOfReturn = (double) unrealizedPnL / buyAmount * 100;
                        dto = AccountsDto.builder()
                            .balance(balance.toPlainString())
                            .avgPrice(avgPrice)
                            .engName(marketCode.getEnglish_name())
                            .korName(marketCode.getKorean_name())
                            .buyAmount(buyAmount)
                            .estimatedValue(estimatedValue)
                            .unrealizedPnL(unrealizedPnL)
                            .rateOfReturn(Math.round(rateOfReturn * 100.0) / 100.0)
                            .build();
                    }
                }
                
                if (dto != null) {
                    accountsDtoList.add(dto);
                }
            }
            
            log.info("accountsDtoList: {}", accountsDtoList);
            return accountsDtoList;
        } catch (JsonProcessingException e) {
            throw new CustomException("JSON 파싱 실패", e);
        }
    }
    
    public List<OrdersDto> orders(Orders orders) {
        return null;
    }
}
