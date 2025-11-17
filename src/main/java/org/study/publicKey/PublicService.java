package org.study.publicKey;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;

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
@RequiredArgsConstructor
public class PublicService {
    private final ObjectMapper objectMapper = new ObjectMapper();
    private final RestClient restClient;
    
    public List<MarketCode> marketAll(){
        
        try {
            ResponseEntity<String> entity = restClient.get().uri(uriBuilder -> uriBuilder.queryParam("isDetails",false).pathSegment("v1","market","all").build())
                .header("accept", "application/json")
                .retrieve()
                .toEntity(String.class);
            // 1) 상태코드 검증
            if (!entity.getStatusCode().is2xxSuccessful()) {
                throw new IllegalStateException("HTTP 에러: " + entity.getStatusCode());
            }
            
            // 2) 바디 검증
            String json = entity.getBody();
            if (json == null || json.isBlank()) {
                throw new IllegalStateException("응답 바디 없음");
            }
            
            // ✅ JSON → 객체
            List<MarketCode> marketCode = objectMapper.readValue(
                json,
                new TypeReference<List<MarketCode>>() {}
            );
            return marketCode.stream()
                .filter(v -> v.getMarket() != null && v.getMarket().contains("KRW"))
                .toList();
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }
}
