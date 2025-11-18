package org.study.publicKey;

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
import org.study.publicKey.marketAll.MarketCode;
import org.study.publicKey.ticker.Ticker;

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
    private final HttpService httpService;
    
    public List<MarketCode> marketAll() {
        try {
            MultiValueMap<String, String> headers = new LinkedMultiValueMap<>();
            headers.add("accept", "application/json");
            
            MultiValueMap<String, String> params = new LinkedMultiValueMap<>();
            params.add("isDetails", "false");
            
            String response = httpService.client("get", "market/all", headers, params);
            
            // ✅ JSON → 객체
            List<MarketCode> marketCode = objectMapper.readValue(
                response,
                new TypeReference<>() {
                }
            );
            return marketCode.stream()
                .filter(v -> v.getMarket() != null && v.getMarket().contains("KRW"))
                .toList();
        } catch (JsonProcessingException e) {
            throw new CustomException("JSON 파싱 실패", e);
        }
    }
    
    public List<Ticker> ticker(String markets) {
        try {
            MultiValueMap<String, String> headers = new LinkedMultiValueMap<>();
            headers.add("accept", "application/json");
            
            MultiValueMap<String, String> params = new LinkedMultiValueMap<>();
            params.add("markets", "KRW-"+markets);
            
            String response = httpService.client("get", "ticker", headers, params);
            // ✅ JSON → 객체
            return objectMapper.readValue(
                response,
                new TypeReference<>() {
                }
            );
        } catch (JsonProcessingException e) {
            throw new CustomException("JSON 파싱 실패", e);
        }
    }
    
}
