package org.study.publicKey;

import io.swagger.v3.oas.annotations.Operation;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
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
@CrossOrigin(origins = "http://localhost:5173")
@RequiredArgsConstructor
public class PublicController {
    
    private final PublicService publicService;
    
    @Operation(
        summary = "마켓 코드 조회",
        description = "빗썸에서 거래 가능한 마켓과 가상자산 정보를 제공합니다."
    )
    @GetMapping("/market/all")
    public ResponseEntity<List<MarketCode>> marketAll(){
        return ResponseEntity.ok(publicService.marketAll());
    }
}
