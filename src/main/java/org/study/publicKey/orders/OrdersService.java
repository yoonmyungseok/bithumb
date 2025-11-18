package org.study.publicKey.orders;

import com.auth0.jwt.JWT;
import com.auth0.jwt.algorithms.Algorithm;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;
import org.study.config.BithumbProperties;

import java.math.BigInteger;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Map;
import java.util.UUID;
import java.util.stream.Collectors;

/**
 * 1. ClassName     : OrdersService
 * 2. FileName      : OrdersService.java
 * 3. Package       : org.study.publicKey.orders
 * 4. Author        : 윤명석
 * 5. Creation Date : 25. 11. 18. 오후 5:52
 * 6. Modified Date : 25. 11. 18. 오후 5:52
 * 7. Comment       :
 */
@Service
@RequiredArgsConstructor
public class OrdersService {
    private final RestClient restClient;
    private final BithumbProperties bithumbProperties;
    
    
    /**
     * Map → query string (key1=val1&key2=val2...) JDK만 사용
     */
    private String buildQuery(Map<String, Object> params) {
        return params.entrySet().stream()
            .map(entry -> {
                try {
                    String key = URLEncoder.encode(entry.getKey(), StandardCharsets.UTF_8);
                    String value = URLEncoder.encode(String.valueOf(entry.getValue()), StandardCharsets.UTF_8);
                    return key + "=" + value;
                } catch (Exception e) {
                    throw new RuntimeException(e);
                }
            })
            .collect(Collectors.joining("&"));
    }
    
    /**
     * 예시용: KRW-BTC 지정가 매수 주문
     */
    public ResponseEntity<String> createLimitBidOrder() throws Exception {
        String accessKey = bithumbProperties.getAccessKey();
        String secretKey = bithumbProperties.getSecretKey();
        // 1) 요청 바디 (NumberString은 문자열로 쓰는 게 안전)
        Map<String, Object> requestBody = Map.of(
            "market", "KRW-XRP",
            "side", "bid",          // 매수
            "volume", "0.001",      // 주문량 (NumberString)
            "price", "84000000",    // 주문가격 (NumberString)
            "ord_type", "limit"     // 지정가
        );
        
        // 2) query_hash 생성 (문서 규격 그대로)
        String query = buildQuery(requestBody);
        
        MessageDigest md = MessageDigest.getInstance("SHA-512");
        byte[] digest = md.digest(query.getBytes(StandardCharsets.UTF_8));
        String queryHash = String.format("%0128x", new BigInteger(1, digest));
        
        // 3) JWT 토큰 생성
        Algorithm algorithm = Algorithm.HMAC256(secretKey);
        String jwtToken = JWT.create()
            .withClaim("access_key", accessKey)
            .withClaim("nonce", UUID.randomUUID().toString())
            .withClaim("timestamp", System.currentTimeMillis())
            .withClaim("query_hash", queryHash)
            .withClaim("query_hash_alg", "SHA512")
            .sign(algorithm);
        
        String authenticationToken = "Bearer " + jwtToken;
        
        // 4) RestClient로 POST 호출
        ResponseEntity<String> response = restClient
            .post()
            .uri("/v1/orders")
            .header(HttpHeaders.AUTHORIZATION, authenticationToken)
            .contentType(MediaType.APPLICATION_JSON)
            .body(requestBody)              // Map → JSON 자동 변환
            .retrieve()
            .toEntity(String.class);
        
        // 로그 확인용
        System.out.println(response.getStatusCode());
        System.out.println(response.getBody());
        
        return response;
    }
}
