package org.study.publicKey;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.extern.slf4j.Slf4j;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.CrossOrigin;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.stream.Collectors;

/**
 * 1. ClassName     : PublicController
 * 2. FileName      : PublicController.java
 * 3. Package       : org.study.publicKey
 * 4. Author        : 윤명석
 * 5. Creation Date : 25. 11. 16. 오후 12:13
 * 6. Modified Date : 25. 11. 16. 오후 12:13
 * 7. Comment       :
 */
@RestController
@RequestMapping("public")
@Slf4j
@CrossOrigin(origins = "http://localhost:3000")
public class PublicController {

    @GetMapping("/market/all")
    public ResponseEntity<List<MarketCode>> 마켓코드조회(){
        OkHttpClient client = new OkHttpClient();
        ObjectMapper objectMapper = new ObjectMapper();
        
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
            List<MarketCode> krw = marketCode.stream()
                .filter(v -> v.getMarket() != null && v.getMarket().contains("KRW"))
                .toList();
            return ResponseEntity.ok(krw);
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }
}
