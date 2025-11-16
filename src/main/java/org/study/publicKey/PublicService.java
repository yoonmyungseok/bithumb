package org.study.publicKey;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.extern.slf4j.Slf4j;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;

import java.util.List;

/**
 * 1. ClassName     : MarketCodeService
 * 2. FileName      : MarketCodeService.java
 * 3. Package       : org.study.publicKey
 * 4. Author        : 윤명석
 * 5. Creation Date : 25. 11. 16. 오후 8:01
 * 6. Modified Date : 25. 11. 16. 오후 8:01
 * 7. Comment       :
 */
@Service
@Slf4j
public class PublicService {
    private final OkHttpClient client = new OkHttpClient();
    private final ObjectMapper objectMapper = new ObjectMapper();
    
    public List<MarketCode> marketCodeList(){
        Request request = new Request.Builder()
            .url("https://api.bithumb.com/v1/market/all?isDetails=false")
            .get()
            .addHeader("accept", "application/json")
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
            List<MarketCode> marketCode = objectMapper.readValue(
                json,
                new TypeReference<List<MarketCode>>() {}
            );
            log.info("마켓코드: {}", marketCode);
            return marketCode.stream()
                .filter(v -> v.getMarket() != null && v.getMarket().contains("KRW"))
                .toList();
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }
}
