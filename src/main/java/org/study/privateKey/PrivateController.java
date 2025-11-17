package org.study.privateKey;

import io.swagger.v3.oas.annotations.Operation;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.CrossOrigin;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 1. ClassName     : PrivateController
 * 2. FileName      : PrivateController.java
 * 3. Package       : org.study.privateKey
 * 4. Author        : 윤명석
 * 5. Creation Date : 25. 11. 16. 오후 7:49
 * 6. Modified Date : 25. 11. 16. 오후 7:49
 * 7. Comment       :
 */
@RestController
@RequestMapping("private")
@Slf4j
@CrossOrigin(origins = "http://localhost:5173")
@RequiredArgsConstructor
public class PrivateController {
    
    private final PrivateService privateService;
    
    @Operation(
        summary = "전체 계좌 조회",
        description = "보유 중인 자산 정보를 조회합니다."
    )
    @GetMapping("/accounts")
    public ResponseEntity<List<AccountsDto>> accounts(){
        return ResponseEntity.ok(privateService.accounts());
    }
}
