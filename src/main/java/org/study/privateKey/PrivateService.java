package org.study.privateKey;

import com.auth0.jwt.JWT;
import com.auth0.jwt.algorithms.Algorithm;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import okhttp3.*;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;
import org.study.GlobalConfig;
import org.study.publicKey.MarketCode;
import org.study.publicKey.PublicService;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

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
    
    public List<AccountsDto> 전체계좌조회(){
        String accessKey = GlobalConfig.ACCESS_KEY;
        String secretKey = GlobalConfig.SECRET_KEY;
        String apiUrl = GlobalConfig.API_URL;
        
        // Generate access token
        Algorithm algorithm = Algorithm.HMAC256(secretKey);
        String jwtToken = JWT.create()
            .withClaim("access_key", accessKey)
            .withClaim("nonce", UUID.randomUUID().toString())
            .withClaim("timestamp", System.currentTimeMillis())
            .sign(algorithm);
        String authenticationToken = "Bearer " + jwtToken;
        
        OkHttpClient client = new OkHttpClient();
        ObjectMapper objectMapper = new ObjectMapper();
        
        // ✅ HttpUrl로 URL 안전하게 생성
        HttpUrl baseUrl = HttpUrl.get(apiUrl);
        HttpUrl url = baseUrl.newBuilder()
            .addPathSegment("v1")
            .addPathSegment("accounts")
            .build();
        
        Request request = new Request.Builder()
            .url(url) // 문자열 대신 HttpUrl 객체
            .addHeader("Authorization", authenticationToken)
            .get()
            .build();
        
        try (Response response = client.newCall(request).execute()) {
            if (!response.isSuccessful()) {
                throw new IllegalStateException("HTTP 에러: " + response.code());
            }
            
            ResponseBody responseBody = response.body();
            if (responseBody == null) {
                throw new IllegalStateException("응답 바디 없음");
            }
            
            String json = responseBody.string();   // ✅ JSON 문자열
            // log.info("응답: {}", json);
            
            // ✅ JSON → 객체
            List<Accounts> accounts = objectMapper.readValue(
                json,
                new TypeReference<List<Accounts>>() {}
            );
            
            List<MarketCode> marketCodeList = publicService.marketCodeList();
            log.info("전체계좌: {}", accounts);
            List<AccountsDto> accountsDtoList = new ArrayList<>();
            for(Accounts account:accounts){
                for(MarketCode marketCode:marketCodeList){
                    if(marketCode.getMarket().substring(4).equals(account.getCurrency())){
                        BigDecimal balance = (new BigDecimal(account.getLocked()).add(new BigDecimal(account.getBalance())));
                        double avgPrice = account.getAvg_buy_price();
                        AccountsDto dto = AccountsDto.builder()
                            .balance(balance.toPlainString())
                            .avgPrice(avgPrice)
                            .engName(marketCode.getEnglish_name())
                            .korName(marketCode.getKorean_name())
                            .buyAmount(balance.multiply(new BigDecimal(Double.toString(avgPrice))).setScale(0, RoundingMode.HALF_UP).intValue())
                            .build();
                        accountsDtoList.add(dto);
                        break;
                    }else if("P".equals(account.getCurrency())){
                        AccountsDto dto = AccountsDto.builder()
                            .balance(account.getBalance())
                            .engName("P")
                            .korName("포인트")
                            .build();
                        accountsDtoList.add(dto);
                        break;
                    }else if("KRW".equals(account.getCurrency())){
                        AccountsDto dto = AccountsDto.builder()
                            .balance(account.getBalance())
                            .korName("원화")
                            .build();
                        accountsDtoList.add(dto);
                        break;
                    }
                }
            }
//            List<MarketCode> krw = accounts.stream()
//                .filter(v -> v.getMarket() != null && v.getMarket().contains("KRW"))
//                .toList();
            log.info("accountsDtoList: {}", accountsDtoList);
            return accountsDtoList;
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }
}
